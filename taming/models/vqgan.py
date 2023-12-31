import hashlib
import logging
import math
from typing import Union
import warnings
import torch
from torch import nn
import torch.nn.functional as F
import pytorch_lightning as pl
from tqdm import tqdm
import urllib

from main import instantiate_from_config

from taming.modules.diffusionmodules.model import Encoder, Decoder, Text_Decoder
from taming.modules.vqvae.quantize import VectorQuantizer2 as VectorQuantizer
from taming.modules.vqvae.quantize import GumbelQuantize
from taming.modules.vqvae.quantize import EMAVectorQuantizer
from taming.modules.transformer.cliptransformer import TextTransformer, Attention
from taming.modules.vqvae.quantize import Cluster
from taming.data.base import tokenize
from taming.modules.tokenizer.simple_tokenizer import SimpleTokenizer as _Tokenizer
from torch.optim.lr_scheduler import StepLR
import os

def download_pretrained_from_url(
        url: str,
        cache_dir: Union[str, None] = None,
):
    os.makedirs(cache_dir, exist_ok=True)
    filename = os.path.basename(url)

    if 'openaipublic' in url:
        expected_sha256 = url.split("/")[-2]
    elif 'mlfoundations' in url:
        expected_sha256 = os.path.splitext(filename)[0].split("-")[-1]
    else:
        expected_sha256 = ''

    download_target = os.path.join(cache_dir, filename)

    if os.path.exists(download_target) and not os.path.isfile(download_target):
        raise RuntimeError(f"{download_target} exists and is not a regular file")

    if os.path.isfile(download_target):
        if expected_sha256:
            if hashlib.sha256(open(download_target, "rb").read()).hexdigest().startswith(expected_sha256):
                return download_target
            else:
                warnings.warn(f"{download_target} exists, but the SHA256 checksum does not match; re-downloading the file")
        else:
            return download_target

    with urllib.request.urlopen(url) as source, open(download_target, "wb") as output:
        with tqdm(total=int(source.headers.get("Content-Length")), ncols=80, unit='iB', unit_scale=True) as loop:
            while True:
                buffer = source.read(8192)
                if not buffer:
                    break

                output.write(buffer)
                loop.update(len(buffer))

    if expected_sha256 and not hashlib.sha256(open(download_target, "rb").read()).hexdigest().startswith(expected_sha256):
        raise RuntimeError(f"Model has been downloaded but the SHA256 checksum does not not match")

    return download_target

def load_state_dict(checkpoint_path: str, map_location='cpu'):
    checkpoint = torch.load(checkpoint_path, map_location=map_location)
    if isinstance(checkpoint, dict) and 'state_dict' in checkpoint:
        state_dict = checkpoint['state_dict']
    else:
        state_dict = checkpoint
    if next(iter(state_dict.items()))[0].startswith('module'):
        state_dict = {k[7:]: v for k, v in state_dict.items()}
    return state_dict

# used to maintain checkpoint compatibility
def convert_to_custom_text_state_dict(state_dict: dict):
    if 'text_projection' in state_dict:
        # old format state_dict, move text tower -> .text
        new_state_dict = {}
        for k, v in state_dict.items():
            if any(k.startswith(p) for p in (
                'text_projection',
                'positional_embedding',
                'token_embedding',
                'transformer',
                'ln_final',
            )):
                k = 'text.' + k
            new_state_dict[k] = v
        return new_state_dict
    return state_dict

def resize_pos_embed(state_dict, pos_embed_width):
    # Resize the shape of position embeddings when loading from state_dict
    pos_emb = state_dict.get('positional_embedding', None)
    pos_emb = pos_emb.unsqueeze(0).permute(0, 2, 1)
    pos_emb = F.interpolate(pos_emb, size=pos_embed_width)
    pos_emb = pos_emb.permute(0, 2, 1).squeeze()
    state_dict['positional_embedding'] = pos_emb


class VQModel(pl.LightningModule):
    def __init__(self,
                 ddconfig,
                 ctconfig,
                 lossconfig,
                 n_embed,
                 embed_dim,
                 ckpt_path=None,
                 ct_ckpt_dir=None,
                 ignore_keys=[],
                 image_key="image",
                 colorize_nlabels=None,
                 monitor=None,
                 remap=None,
                 sane_index_shape=False,  # tell vector quantizer to return indices as bhw
                 ):
        super().__init__()
        # 图像侧
        self.image_key = image_key
        self.encoder = Encoder(**ddconfig)
        self.decoder = Decoder(**ddconfig)  # 图像与文本侧解码器部分共用
        self.loss = instantiate_from_config(lossconfig)
        self.quantize = VectorQuantizer(n_embed, embed_dim, beta=0.25,
                                        remap=remap, sane_index_shape=sane_index_shape)
        self.quant_conv = torch.nn.Conv2d(ddconfig["z_channels"], embed_dim, 1)
        self.post_quant_conv = torch.nn.Conv2d(embed_dim, ddconfig["z_channels"], 1)
        if ckpt_path is not None:
            self.init_from_ckpt(ckpt_path, ignore_keys=ignore_keys)
        self.image_key = image_key
        if colorize_nlabels is not None:
            assert type(colorize_nlabels)==int
            self.register_buffer("colorize", torch.randn(3, colorize_nlabels, 1, 1))
        # 文本侧
        self.text_encoder = TextTransformer(**ctconfig)
        self.text_decoder = Text_Decoder(ctconfig["vocab_size"])  # 图像与文本侧解码器部分共用
        self.quant_linear = nn.Linear(ctconfig["width"], embed_dim)       # 从文本侧的宽度映射到coodbook的宽度
        self.quant_tf = nn.TransformerDecoder(nn.TransformerDecoderLayer(d_model=256, nhead=8), num_layers=6)
        self.post_quant_tf = nn.TransformerEncoder(nn.TransformerEncoderLayer(d_model=256, nhead=8), num_layers=2)
        if ct_ckpt_dir is not None:
            self.init_from_ct_ckpt(ct_ckpt_dir, self.text_encoder, ctconfig["context_length"], ignore_keys=ignore_keys)
        #图像与文本交叉量化
        self.i_t_cluster = Cluster(embed_dim)

        if monitor is not None:
            self.monitor = monitor

    def init_from_ckpt(self, path, ignore_keys=list()):
        sd = torch.load(path, map_location="cpu")["state_dict"]
        keys = list(sd.keys())
        for k in keys:
            for ik in ignore_keys:
                if k.startswith(ik):
                    print("Deleting key {} from state_dict.".format(k))
                    del sd[k]
        self.load_state_dict(sd, strict=False)
        print(f"Restored from {path}")

    def init_from_ct_ckpt(self, path, model, pos_embed_width, ignore_keys=list()):
        url = 'https://github.com/mlfoundations/open_clip/releases/download/v0.2-weights/vit_b_32-quickgelu-laion400m_e32-46683a32.pt'
        cache_dir = path
        checkpoint_path = download_pretrained_from_url(url, cache_dir=cache_dir)
        state_dict = load_state_dict(checkpoint_path)
        # detect old format and make compatible with new format
        if 'positional_embedding' in state_dict and not hasattr(model, 'positional_embedding'):
            state_dict = convert_to_custom_text_state_dict(state_dict)
        resize_pos_embed(state_dict, pos_embed_width)
        incompatible_keys = model.load_state_dict(state_dict, strict=False)
        return incompatible_keys        
        

    # 图像侧
    def encode(self, x):
        h = self.encoder(x)
        image_hidden = self.quant_conv(h)
        image_quant, image_quant_loss, info = self.quantize(image_hidden, key='image')
        return image_quant, image_quant_loss, info, image_hidden

    def decode(self, image_coquant):
        image_coquant = self.post_quant_conv(image_coquant)
        #图像->图像
        i2i_rec, image_hidden = self.decoder(image_coquant)                      # [8, 3, 256, 256]
        #图像->文本
        i2t_rec = self.text_decoder(image_hidden)     # [8, 256, 49408]
        return i2i_rec, i2t_rec

    def decode_code(self, code_b):
        quant_b = self.quantize.embed_code(code_b)
        dec = self.decode(quant_b)
        return dec

    #文本侧
    def text_encode(self, text, valid_lens, image_hidden):
        image_hidden = image_hidden.reshape(image_hidden.size(0), image_hidden.size(1), -1)
        image_hidden = image_hidden.permute(2, 0, 1)
        text, mask = self.text_encoder(text, valid_lens)              #[bs, l, d]  [8, 256, 256], mask在图像替换时使用
        text = self.quant_linear(text).permute(1, 0, 2)
        text = self.quant_tf(tgt=image_hidden, memory=text).permute(1, 0, 2)
        text_quant, text_quant_loss, codebook_indices = self.quantize(text, key='text')     #quant: [8, 256, 256]
        return text_quant, text_quant_loss, codebook_indices, text, mask

    def text_decode(self, text_quant, valid_lens):             # [bs, l, d]  [8, 256, 256]
        text_quant = text_quant.permute(1, 0, 2)                    # [l, bs, d]
        text_quant = self.post_quant_tf(text_quant)      # self-attention
        text_quant = text_quant.permute(1, 2, 0)                    # [8, 256, 256], [bs, d, l]
        b, d, l = text_quant.size()
        text_quant = text_quant.reshape(b, d, 16, 16) # [8, 256, 16, 16]

        #文本->图像
        t2i_rec, text_hidden = self.decoder(text_quant)
        #文本->文本
        t2t_rec = self.text_decoder(text_hidden)                              # [8, 256, 49408]

        return t2t_rec, t2i_rec
    
    def forward(self, image, text, valid_lens):
        image_quant, image_quant_loss, info, image_hidden = self.encode(image)
        text_quant,  text_quant_loss, text_codebook_indices, text_hidden, mask = self.text_encode(text, valid_lens, image_hidden)

        image_coquant, text_coquant, i_coquant_loss, t_coquant_loss = self.i_t_cluster(image_quant, text_quant, mask, valid_lens)           # [8, 256, 16, 16],  [8, 256, 256]

        i2i_rec, i2t_rec = self.decode(image_coquant)              # [8, 3, 256, 256],   [8, 256, 49408]
        t2t_rec, t2i_rec  = self.text_decode(text_quant, valid_lens)        #不是整数, [8, 256, 49408],   [8, 3, 256, 256]
        return i2i_rec, i2t_rec, image_quant_loss, t2t_rec, t2i_rec, text_quant_loss,  i_coquant_loss, t_coquant_loss        # q_loss 是文本与图像相互替换时的损失

    def get_input(self, batch, k):
        image = batch['image']                                #[8, 256, 256, 3]
        text, valid_lens = tokenize(list(batch['caption'][0]), context_length=256)  #[8, 1, 256]
        text = text.to(image.device)
        valid_lens = valid_lens.to(image.device)
        if len(text.shape) == 3:
            text = torch.squeeze(text, 1)
        if len(image.shape) == 3:
            image = image[..., None]
        image = image.permute(0, 3, 1, 2).to(memory_format=torch.contiguous_format)
        return image.float(), text, valid_lens                            #[8, 3, 256, 256], [8, 256]

    def training_step(self, batch, batch_idx, optimizer_idx):
        image, text, valid_lens = self.get_input(batch, self.image_key)
        i2i_rec, i2t_rec, image_quant_loss, t2t_rec, t2i_rec, text_quant_loss, i_coquant_loss, t_coquant_loss = self(image, text, valid_lens)

        if optimizer_idx == 0:
            # autoencode
            loss, log_dict = self.loss(image_quant_loss, image, i2i_rec, i2t_rec, optimizer_idx, self.global_step, text, t2i_rec, t2t_rec, text_quant_loss, valid_lens, i_coquant_loss, t_coquant_loss,   #返回loss和日志形式的字典
                                            last_layer=self.get_last_layer(), split="train")

            self.log("train/loss", loss, prog_bar=True, logger=True, on_step=True, on_epoch=True)
            self.log_dict(log_dict, prog_bar=False, logger=True, on_step=True, on_epoch=True)
            return loss

        if optimizer_idx == 1:
            # discriminator
            discloss, log_dict_disc = self.loss(image_quant_loss, image, i2i_rec, i2t_rec, optimizer_idx, self.global_step, text, t2i_rec, t2t_rec, text_quant_loss, valid_lens, i_coquant_loss, t_coquant_loss, 
                                            last_layer=self.get_last_layer(), split="train")
            self.log("train/discloss", discloss, prog_bar=True, logger=True, on_step=True, on_epoch=True)
            self.log_dict(log_dict_disc, prog_bar=False, logger=True, on_step=True, on_epoch=True)
            return discloss

    def validation_step(self, batch, batch_idx):
        image, text, valid_lens = self.get_input(batch, self.image_key) #torch.Size([3, 3, 256, 256]), torch.Size([3, 1, 77])
        i2i_rec, i2t_rec, image_quant_loss, t2t_rec, t2i_rec, text_quant_loss, i_coquant_loss, t_coquant_loss = self(image, text, valid_lens)                           #AE重构图，codebook损失

        loss, log_dict = self.loss(image_quant_loss, image, i2i_rec, i2t_rec, 0, self.global_step, text, t2i_rec, t2t_rec, text_quant_loss, valid_lens, i_coquant_loss, t_coquant_loss,       #生成器的验证损失，以及字典形式的损失日志
                                            last_layer=self.get_last_layer(), split="val")

        discloss, log_dict_disc = self.loss(image_quant_loss, image, i2i_rec, i2t_rec, 1, self.global_step, text, t2i_rec, t2t_rec, text_quant_loss, valid_lens, i_coquant_loss, t_coquant_loss,  #判别器的验证损失日志
                                            last_layer=self.get_last_layer(), split="val")
        
        #log：像是TensorBoard等log记录器，对于每个log的标量，都会有一个相对应的横坐标，它可能是batch number或epoch number
        #on_step就表示把这个log出去的量的横坐标表示为当前batch，而on_epoch则表示将log的量在整个epoch上进行累积后log，横坐标为当前epoch
        self.log("val/loss", loss,
                   prog_bar=True, logger=True, on_step=False, on_epoch=True, sync_dist=True)
        self.log("val/disc_total_loss", discloss,
                   prog_bar=True, logger=True, on_step=False, on_epoch=True, sync_dist=True)             
        self.log_dict(log_dict)              #log_dict：和log函数唯一的区别就是，name和value变量由一个字典替换。表示同时log多个值
        self.log_dict(log_dict_disc)
        return self.log_dict                    #没有返回值限制，不一定非要输出一个val_loss    

    @staticmethod
    def set_bn_eval(module):
        if isinstance(module, nn.BatchNorm2d):
            module.eval()

    def freeze(self, module):
        for name, param in module.named_parameters():
            param.requires_grad = False
            

    def configure_optimizers(self):
        lr = self.learning_rate
        # self.freeze(self.encoder)
        # self.encoder.apply(self.set_bn_eval)

        # self.freeze(self.decoder)
        # self.decoder.apply(self.set_bn_eval)
        # for name, param in self.decoder.conv_out.named_parameters():
        #     param.requires_grad = True     

        # self.freeze(self.quantize)
        # self.quantize.apply(self.set_bn_eval)

        # self.freeze(self.quant_conv)
        # self.quant_conv.apply(self.set_bn_eval)

        # self.freeze(self.post_quant_conv)
        # self.post_quant_conv.apply(self.set_bn_eval)

        # self.freeze(self.text_encoder)
        # self.text_encoder.apply(self.set_bn_eval)

        # 获取需要更新的参数
        opt_vq_params = []
        opt_disc_params = []
        for name, param in self.named_parameters():
            if param.requires_grad:
                # 将参数分别添加到对应的优化器参数列表中
                if name.startswith('loss.discriminator'):
                    opt_disc_params.append(param)
                else:
                    opt_vq_params.append(param)

        opt_vq = torch.optim.Adam(opt_vq_params,lr=lr, betas=(0.5, 0.9))
        opt_disc = torch.optim.Adam(opt_disc_params,lr=lr, betas=(0.5, 0.9))
        
        return [opt_vq, opt_disc], []

    def get_last_layer(self):
        return self.decoder.conv_out.weight

    def log_images(self, batch, **kwargs):
        log = dict()
        ori_text = []
        for i in range(len(batch['caption'][0])):
            ori_text.append(batch['caption'][0][i])
        image, text, valid_lens = self.get_input(batch, self.image_key)       #tuple(image, text)
        image = image.to(self.device)
        text = text.to(self.device)
        i2i_rec, i2t_rec, image_q_loss , t2t_rec, t2i_rec, text_q_loss, i_loss, t_loss = self(image, text, valid_lens)
        ###
        image_quant2, _, _, image_hidden = self.encode(image)
        text_quant2, _, _, text_hidden, _ = self.text_encode(text, valid_lens, image_hidden)
        i2i_no_cq_rec, _ = self.decode(image_quant2)
        _, t2i_no_cq_rec = self.text_decode(text_quant2, valid_lens)
        ###
        if image.shape[1] > 3:
            # colorize with random projection
            assert i2i_rec.shape[1] > 3
            image = self.to_rgb(image)              #图像的原始输入
            i2i_rec = self.to_rgb(i2i_rec)          #图像重建的图像
            t2i_rec = self.to_rgb(t2i_rec)          #文本重建的图像
        # 文本恢复成自然语言
        t2t_rec = torch.max(t2t_rec, 2)[1]          #返回最大值的索引
        i2t_rec = torch.max(i2t_rec, 2)[1]
        _tokenizer = _Tokenizer()
        t2t_rec = t2t_rec.cpu().numpy().tolist()
        i2t_rec = i2t_rec.cpu().numpy().squeeze().tolist()
        t2t = []
        i2t = []
        for i in range(len(t2t_rec)):
            t2t.append(_tokenizer.decode(t2t_rec[i][:int(valid_lens[i])]))
            i2t.append(_tokenizer.decode(i2t_rec[i][:int(valid_lens[i])]))

        log["inputs"] = image                       #[n,3,256,256]
        log["text_inputs"] = ori_text
        log["i2i_reconstructions"] = i2i_rec        #[n,3,256,256]
        log['t2i_reconstructions'] = t2i_rec
        log['t2t_reconstructions'] = t2t
        log['i2t_reconstructions'] = i2t
        ###
        log['i2i_no_cq_rec'] = i2i_no_cq_rec
        log['t2i_no_cq_rec'] = t2i_no_cq_rec
        ###        
        return log