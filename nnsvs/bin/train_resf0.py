from pathlib import Path

import hydra
import mlflow
import torch
from hydra.utils import to_absolute_path
from nnsvs.base import PredictionType
from nnsvs.mdn import mdn_get_most_probable_sigma_and_mu, mdn_loss
from nnsvs.multistream import get_static_features, split_streams
from nnsvs.train_util import (
    check_resf0_config,
    compute_batch_pitch_regularization_weight,
    compute_distortions,
    compute_ms_loss,
    eval_spss_model,
    log_params_from_omegaconf_dict,
    save_checkpoint,
    save_configs,
    setup,
)
from nnsvs.util import PyTorchStandardScaler, make_non_pad_mask
from omegaconf import DictConfig
from torch import nn
from tqdm import tqdm


def train_step(
    model,
    model_config,
    optimizer,
    train,
    in_feats,
    out_feats,
    lengths,
    out_scaler,
    pitch_reg_dyn_ws,
    pitch_reg_weight=1.0,
    ms_streams=None,
    ms_use_static_feats_only=True,
    ms_weight=1.0,
):
    optimizer.zero_grad()
    log_metrics = {}

    criterion = nn.MSELoss(reduction="none")
    prediction_type = (
        model.module.prediction_type()
        if isinstance(model, nn.DataParallel)
        else model.prediction_type()
    )
    is_autoregressive = (
        model.module.is_autoregressive()
        if isinstance(model, nn.DataParallel)
        else model.is_autoregressive()
    )

    # Apply preprocess if required (e.g., FIR filter for shallow AR)
    # defaults to no-op
    if isinstance(model, nn.DataParallel):
        out_feats = model.module.preprocess_target(out_feats)
    else:
        out_feats = model.preprocess_target(out_feats)

    # Run forward
    if is_autoregressive:
        pred_out_feats, lf0_residual = model(in_feats, lengths, out_feats)
    else:
        pred_out_feats, lf0_residual = model(in_feats, lengths)

    # Mask (B, T, 1)
    mask = make_non_pad_mask(lengths).unsqueeze(-1).to(in_feats.device)

    # Compute loss
    if prediction_type == PredictionType.PROBABILISTIC:
        pi, sigma, mu = pred_out_feats

        # (B, max(T)) or (B, max(T), D_out)
        mask_ = mask if len(pi.shape) == 4 else mask.squeeze(-1)
        # Compute loss and apply mask
        loss_feats = mdn_loss(pi, sigma, mu, out_feats, reduce=False)
        loss_feats = loss_feats.masked_select(mask_).mean()
    else:
        loss_feats = criterion(
            pred_out_feats.masked_select(mask), out_feats.masked_select(mask)
        ).mean()

    # Pitch regularization
    # NOTE: l1 loss seems to be better than mse loss in my experiments
    # we could use l2 loss as suggested in the sinsy's paper
    loss_pitch = (pitch_reg_dyn_ws * lf0_residual.abs()).masked_select(mask).mean()

    # MS loss
    loss_ms = 0
    if ms_weight > 0:
        if ms_use_static_feats_only:
            ms_out_feats = get_static_features(
                out_feats,
                model_config.num_windows,
                model_config.stream_sizes,
                model_config.has_dynamic_features,
                ms_streams,
            )
            ms_pred_out_feats = get_static_features(
                pred_out_feats,
                model_config.num_windows,
                model_config.stream_sizes,
                model_config.has_dynamic_features,
                ms_streams,
            )
        else:
            ms_out_feats = split_streams(
                out_feats, model_config.stream_sizes, ms_streams
            )
            ms_pred_out_feats = split_streams(
                pred_out_feats,
                model_config.stream_sizes,
                ms_streams,
            )
        # Stream-wise MS loss
        for ms_out_feats_, ms_pred_out_feats_ in zip(ms_out_feats, ms_pred_out_feats):
            loss_ms += compute_ms_loss(ms_out_feats_, ms_pred_out_feats_)

    loss = loss_feats + pitch_reg_weight * loss_pitch + ms_weight * loss_ms

    if prediction_type == PredictionType.PROBABILISTIC:
        with torch.no_grad():
            pred_out_feats_ = mdn_get_most_probable_sigma_and_mu(pi, sigma, mu)[1]
    else:
        pred_out_feats_ = pred_out_feats
    distortions = compute_distortions(
        pred_out_feats_, out_feats, lengths, out_scaler, model_config
    )

    if train:
        loss.backward()
        optimizer.step()

    log_metrics.update(distortions)
    log_metrics.update(
        {
            "Loss": loss.item(),
            "Loss_Feats": loss_feats.item(),
            "Loss_MS": loss_ms.item(),
            "Loss_Pitch": loss_pitch.item(),
        }
    )

    return loss, log_metrics


def train_loop(
    config,
    logger,
    device,
    model,
    optimizer,
    lr_scheduler,
    data_loaders,
    writer,
    in_scaler,
    out_scaler,
    use_mlflow,
):
    out_dir = Path(to_absolute_path(config.train.out_dir))
    best_dev_loss = torch.finfo(torch.float32).max
    last_dev_loss = torch.finfo(torch.float32).max

    in_lf0_idx = config.data.in_lf0_idx
    in_rest_idx = config.data.in_rest_idx
    if in_lf0_idx is None or in_rest_idx is None:
        raise ValueError("in_lf0_idx and in_rest_idx must be specified")
    pitch_reg_weight = config.train.pitch_reg_weight

    for epoch in tqdm(range(1, config.train.nepochs + 1)):
        for phase in data_loaders.keys():
            train = phase.startswith("train")
            model.train() if train else model.eval()
            running_loss = 0
            running_metrics = {}
            evaluated = False
            for in_feats, out_feats, lengths in data_loaders[phase]:
                # NOTE: This is needed for pytorch's PackedSequence
                lengths, indices = torch.sort(lengths, dim=0, descending=True)
                in_feats, out_feats = (
                    in_feats[indices].to(device),
                    out_feats[indices].to(device),
                )
                if (not train) and (not evaluated):
                    eval_spss_model(
                        epoch,
                        model,
                        in_feats,
                        out_feats,
                        lengths,
                        config.model,
                        out_scaler,
                        writer,
                        sr=48000,
                    )
                    evaluated = True

                # Compute denormalized log-F0 in the musical scores
                # test - s.min_[in_lf0_idx]) / s.scale_[in_lf0_idx]
                lf0_score_denorm = (
                    in_feats[:, :, in_lf0_idx] - in_scaler.min_[in_lf0_idx]
                ) / in_scaler.scale_[in_lf0_idx]
                # Fill zeros for rest and padded frames
                lf0_score_denorm *= (in_feats[:, :, in_rest_idx] <= 0).float()
                for idx, length in enumerate(lengths):
                    lf0_score_denorm[idx, length:] = 0
                # Compute time-variant pitch regularization weight vector
                pitch_reg_dyn_ws = compute_batch_pitch_regularization_weight(
                    lf0_score_denorm
                )

                loss, log_metrics = train_step(
                    model=model,
                    model_config=config.model,
                    optimizer=optimizer,
                    train=train,
                    in_feats=in_feats,
                    out_feats=out_feats,
                    lengths=lengths,
                    out_scaler=out_scaler,
                    pitch_reg_dyn_ws=pitch_reg_dyn_ws,
                    pitch_reg_weight=pitch_reg_weight,
                    ms_streams=config.train.ms_streams,
                    ms_use_static_feats_only=config.train.ms_use_static_feats_only,
                    ms_weight=config.train.ms_weight,
                )
                running_loss += loss.item()
                for k, v in log_metrics.items():
                    try:
                        running_metrics[k] += float(v)
                    except KeyError:
                        running_metrics[k] = float(v)

            ave_loss = running_loss / len(data_loaders[phase])
            logger.info("[%s] [Epoch %s]: loss %s", phase, epoch, ave_loss)
            if writer is not None:
                writer.add_scalar(f"Loss/{phase}", ave_loss, epoch)
            if use_mlflow:
                mlflow.log_metric(f"{phase}_loss", ave_loss, step=epoch)

            for k, v in running_metrics.items():
                ave_v = v / len(data_loaders[phase])
                if writer is not None:
                    writer.add_scalar(f"{k}/{phase}", ave_v, epoch)
                if use_mlflow:
                    mlflow.log_metric(f"{phase}_{k}", ave_v, step=epoch)

            if not train:
                last_dev_loss = ave_loss
            if not train and ave_loss < best_dev_loss:
                best_dev_loss = ave_loss
                save_checkpoint(
                    logger, out_dir, model, optimizer, lr_scheduler, epoch, is_best=True
                )

        lr_scheduler.step()
        if epoch % config.train.checkpoint_epoch_interval == 0:
            save_checkpoint(
                logger, out_dir, model, optimizer, lr_scheduler, epoch, is_best=False
            )

    save_checkpoint(
        logger, out_dir, model, optimizer, lr_scheduler, config.train.nepochs
    )
    logger.info("The best loss was %s", best_dev_loss)
    if use_mlflow:
        mlflow.log_metric("best_dev_loss", best_dev_loss, step=epoch)
        mlflow.log_artifacts(out_dir)

    return last_dev_loss


@hydra.main(config_path="conf/train_resf0", config_name="config")
def my_app(config: DictConfig) -> None:
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    (
        model,
        optimizer,
        lr_scheduler,
        data_loaders,
        writer,
        logger,
        in_scaler,
        out_scaler,
    ) = setup(config, device)

    check_resf0_config(logger, model, config, in_scaler, out_scaler)

    out_scaler = PyTorchStandardScaler(
        torch.from_numpy(out_scaler.mean_), torch.from_numpy(out_scaler.scale_)
    ).to(device)
    use_mlflow = config.mlflow.enabled

    if use_mlflow:
        with mlflow.start_run() as run:
            # NOTE: modify out_dir when running with mlflow
            config.train.out_dir = f"{config.train.out_dir}/{run.info.run_id}"
            save_configs(config)
            log_params_from_omegaconf_dict(config)
            last_dev_loss = train_loop(
                config,
                logger,
                device,
                model,
                optimizer,
                lr_scheduler,
                data_loaders,
                writer,
                in_scaler,
                out_scaler,
                use_mlflow,
            )
    else:
        save_configs(config)
        last_dev_loss = train_loop(
            config,
            logger,
            device,
            model,
            optimizer,
            lr_scheduler,
            data_loaders,
            writer,
            in_scaler,
            out_scaler,
            use_mlflow,
        )

    return last_dev_loss


def entry():
    my_app()


if __name__ == "__main__":
    my_app()
