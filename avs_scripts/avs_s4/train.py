import os
import time
import random
import shutil
import torch
import numpy as np
import argparse
import logging
import pytorch_lightning as pl
from pytorch_lightning.lite import LightningLite

from config import cfg
from dataloader import S4Dataset
from torchvggish import vggish
from loss import IouSemanticAwareLoss

from utils import pyutils
from utils.utility import logger, mask_iou
from utils.system import setup_logging


class AudioExtractor(torch.nn.Module):
    def __init__(self, cfg):
        super().__init__()
        self.audio_backbone = vggish.VGGish(cfg)

    def forward(self, audio):
        audio_fea = self.audio_backbone(audio)
        return audio_fea


class Lite(LightningLite):
    def run(self, args):
        if (args.visual_backbone).lower() == "resnet":
            from model import ResNet_AVSModel as AVSModel
            print("==> Use ResNet50 as the visual backbone...")
        elif (args.visual_backbone).lower() == "pvt":
            from model import PVT_AVSModel as AVSModel
            print("==> Use pvt-v2 as the visual backbone...")
        else:
            raise NotImplementedError("only support the resnet50 and pvt-v2")

        # Model
        model = AVSModel.Pred_endecoder(
            channel=256,
            config=cfg,
            tpavi_stages=args.tpavi_stages,
            tpavi_vv_flag=args.tpavi_vv_flag,
            tpavi_va_flag=args.tpavi_va_flag,
        )

        # audio backbone
        audio_backbone = AudioExtractor(cfg)
        audio_backbone.eval()
        for param in audio_backbone.parameters():
            param.requires_grad = False
        self.to_device(audio_backbone)

        # Optimizer
        optimizer = torch.optim.Adam(model.parameters(), args.lr)
        
        model, optimizer = self.setup(model, optimizer)

        # losses
        avg_meter_total_loss = pyutils.AverageMeter("total_loss")
        avg_meter_iou_loss = pyutils.AverageMeter("iou_loss")
        avg_meter_sa_loss = pyutils.AverageMeter("sa_loss")
        avg_meter_miou = pyutils.AverageMeter("miou")

        # Data
        train_dataset = S4Dataset("train")
        train_dataloader = torch.utils.data.DataLoader(
            train_dataset,
            batch_size=args.train_batch_size,
            shuffle=True,
            num_workers=args.num_workers,
            pin_memory=True,
        )
        max_step = (len(train_dataset) // args.train_batch_size) * args.max_epoches

        val_dataset = S4Dataset("val")
        val_dataloader = torch.utils.data.DataLoader(
            val_dataset,
            batch_size=args.val_batch_size,
            shuffle=False,
            num_workers=args.num_workers,
            pin_memory=True,
        )

        # Train
        best_epoch = 0
        global_step = 0
        miou_list = []
        max_miou = 0
        model.train()
        # audio_backbone.eval()
        for epoch in range(args.max_epoches):
            for n_iter, batch_data in enumerate(train_dataloader):
                # [bs, 5, 3, 224, 224], [bs, 5, 1, 96, 64], [bs, 1, 1, 224, 224]
                imgs, audio, mask = batch_data

                # imgs = imgs.cuda()
                # audio = audio.cuda()
                # mask = mask.cuda()
                B, frame, C, H, W = imgs.shape
                imgs = imgs.view(B * frame, C, H, W)
                mask = mask.view(B, H, W)
                # [B*T, 1, 96, 64]
                audio = audio.view(-1, audio.shape[2], audio.shape[3], audio.shape[4])
                audio_feature = audio_backbone(audio)  # [B*T, 128]

                output, visual_map_list, a_fea_list = model(
                    imgs, audio_feature
                )  # [bs*5, 1, 224, 224]
                loss, loss_dict = IouSemanticAwareLoss(
                    output,
                    mask.unsqueeze(1).unsqueeze(1),
                    a_fea_list,
                    visual_map_list,
                    lambda_1=args.lambda_1,
                    count_stages=args.sa_loss_stages,
                    sa_loss_flag=args.sa_loss_flag,
                    mask_pooling_type=args.mask_pooling_type,
                )

                avg_meter_total_loss.add({"total_loss": loss.item()})
                avg_meter_iou_loss.add({"iou_loss": loss_dict["iou_loss"]})
                avg_meter_sa_loss.add({"sa_loss": loss_dict["sa_loss"]})

                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

                global_step += 1

                if (global_step - 1) % 50 == 0:
                    train_log = (
                        "Iter:%5d/%5d, Total_Loss:%.4f, iou_loss:%.4f, sa_loss:%.4f, lambda_1:%.4f, lr: %.4f"
                        % (
                            global_step - 1,
                            max_step,
                            avg_meter_total_loss.pop("total_loss"),
                            avg_meter_iou_loss.pop("iou_loss"),
                            avg_meter_sa_loss.pop("sa_loss"),
                            args.lambda_1,
                            optimizer.param_groups[0]["lr"],
                        )
                    )
                    # train_log = ['Iter:%5d/%5d' % (global_step - 1, max_step),
                    #         'Total_Loss:%.4f' % (avg_meter_loss.pop('total_loss')),
                    #         'iou_loss:%.4f' % (avg_meter_iou_loss.pop('iou_loss')),
                    #         'sa_loss:%.4f' % (avg_meter_sa_loss.pop('sa_loss')),
                    #         'lambda_1:%.4f' % (args.lambda_1),
                    #         'lr: %.4f' % (optimizer.param_groups[0]['lr'])]
                    # print(train_log, flush=True)
                    logger.info(train_log)

            # Validation:
            model.eval()
            with torch.no_grad():
                for n_iter, batch_data in enumerate(val_dataloader):
                    # [bs, 5, 3, 224, 224], [bs, 5, 1, 96, 64], [bs, 5, 1, 224, 224]
                    imgs, audio, mask, _, _ = batch_data

                    imgs = imgs.cuda()
                    audio = audio.cuda()
                    mask = mask.cuda()
                    B, frame, C, H, W = imgs.shape
                    imgs = imgs.view(B * frame, C, H, W)
                    mask = mask.view(B * frame, H, W)
                    audio = audio.view(-1, audio.shape[2], audio.shape[3], audio.shape[4])
                    audio_feature = audio_backbone(audio)

                    # [bs*5, 1, 224, 224]
                    output, _, _ = model(imgs, audio_feature)

                    miou = mask_iou(output.squeeze(1), mask)
                    avg_meter_miou.add({"miou": miou})

                miou = avg_meter_miou.pop("miou")
                if miou > max_miou:
                    model_save_path = os.path.join(
                        checkpoint_dir, "%s_best.pth" % (args.session_name)
                    )
                    torch.save(model.module.state_dict(), model_save_path)
                    best_epoch = epoch
                    logger.info("save best model to %s" % model_save_path)

                miou_list.append(miou)
                max_miou = max(miou_list)

                val_log = "Epoch: {}, Miou: {}, maxMiou: {}".format(epoch, miou, max_miou)
                # print(val_log)
                logger.info(val_log)

            model.train()
        logger.info("best val Miou {} at peoch: {}".format(max_miou, best_epoch))


def main(args):
    pl.seed_everything(args.seed, workers=True)

    # Log directory
    if not os.path.exists(args.log_dir):
        os.makedirs(args.log_dir, exist_ok=True)
    # Logs
    prefix = args.session_name
    log_dir = os.path.join(
        args.log_dir, "{}".format(time.strftime(prefix + "_%Y%m%d-%H%M%S"))
    )
    args.log_dir = log_dir

    # Save scripts
    script_path = os.path.join(log_dir, "scripts")
    if not os.path.exists(script_path):
        os.makedirs(script_path, exist_ok=True)

    scripts_to_save = [
        "train.sh",
        "train.py",
        "test.sh",
        "test.py",
        "config.py",
        "dataloader.py",
        "./model/ResNet_AVSModel.py",
        "./model/PVT_AVSModel.py",
        "loss.py",
    ]
    for script in scripts_to_save:
        dst_path = os.path.join(script_path, script)
        try:
            shutil.copy(script, dst_path)
        except IOError:
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            shutil.copy(script, dst_path)

    # Checkpoints directory
    checkpoint_dir = os.path.join(log_dir, "checkpoints")
    if not os.path.exists(checkpoint_dir):
        os.makedirs(checkpoint_dir, exist_ok=True)
    args.checkpoint_dir = checkpoint_dir

    # Set logger
    log_path = os.path.join(log_dir, "log")
    if not os.path.exists(log_path):
        os.makedirs(log_path, exist_ok=True)

    setup_logging(filename=os.path.join(log_path, "log.txt"))
    logger = logging.getLogger(__name__)
    logger.info("==> Config: {}".format(cfg))
    logger.info("==> Arguments: {}".format(args))
    logger.info("==> Experiment: {}".format(args.session_name))

    # Do training
    Lite(accelerator='gpu', strategy='ddp', devices=2).run(args)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--session_name", default="S4", type=str, help="the S4 setting")
    parser.add_argument(
        "--visual_backbone",
        default="resnet",
        type=str,
        help="use resnet50 or pvt-v2 as the visual backbone",
    )

    parser.add_argument("--train_batch_size", default=4, type=int)
    parser.add_argument("--val_batch_size", default=1, type=int)
    parser.add_argument("--max_epoches", default=15, type=int)
    parser.add_argument("--lr", default=0.0001, type=float)
    parser.add_argument("--num_workers", default=8, type=int)
    parser.add_argument("--wt_dec", default=5e-4, type=float)
    parser.add_argument("--seed", default=123, type=int)

    parser.add_argument(
        "--sa_loss_flag",
        action="store_true",
        default=False,
        help="additional loss for last four frames",
    )
    parser.add_argument(
        "--lambda_1", default=0, type=float, help="weight for balancing l4 loss"
    )
    parser.add_argument(
        "--sa_loss_stages",
        default=[],
        nargs="+",
        type=int,
        help="compute sa loss in which stages: [0, 1, 2, 3",
    )
    parser.add_argument(
        "--mask_pooling_type",
        default="avg",
        type=str,
        help="the manner to downsample predicted masks",
    )

    parser.add_argument(
        "--tpavi_stages",
        default=[],
        nargs="+",
        type=int,
        help="add tpavi block in which stages: [0, 1, 2, 3",
    )
    parser.add_argument(
        "--tpavi_vv_flag",
        action="store_true",
        default=False,
        help="visual-visual self-attention",
    )
    parser.add_argument(
        "--tpavi_va_flag",
        action="store_true",
        default=False,
        help="visual-audio cross-attention",
    )

    parser.add_argument("--weights", type=str, default="", help="path of trained model")
    parser.add_argument("--log_dir", default="./train_logs", type=str)

    args = parser.parse_args()

    main(args)
