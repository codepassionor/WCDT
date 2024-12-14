#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@Project: WcDT
@Name: diffusion.py
@Author: YangChen
@Date: 2023/12/27
"""
from functools import partial
from typing import List

import numpy as np
import torch
from torch import nn
import torch.nn.functional as F

from net_works.transformer import TransformerCrossAttention


def extract(a, t, x_shape):
    t = t.to(torch.long)
    b, *_ = t.shape
    out = a.gather(-1, t)
    return out.reshape(b, *((1,) * (len(x_shape) - 1)))


class LinearLayer(nn.Module):
    def __init__(self, input_dim: int, output_dim: int):
        super().__init__()
        self.layer = nn.Sequential(
            nn.Linear(input_dim, output_dim, bias=True, dtype=torch.float32),
            nn.LeakyReLU(inplace=True)
        )
        self.normal = nn.BatchNorm1d(output_dim)

    def forward(self, x):
        linear_output = self.layer(x)
        linear_output = torch.transpose(linear_output, -1, -2)
        normal_output = self.normal(linear_output)
        return torch.transpose(normal_output, -1, -2)


class Decoder(nn.Module):
    def __init__(self, input_dim: int, middle_dim: int, output_dim: int):
        super(Decoder, self).__init__()
        self.up = LinearLayer(input_dim, output_dim)
        self.cat_layer = LinearLayer(middle_dim, output_dim)

    def forward(self, x1, x2):
        up_output = self.up(x1)
        cat_output = torch.cat((up_output, x2), dim=-1)
        output = self.cat_layer(cat_output)
        return output


class UnetDiffusionModel(nn.Module):
    def __init__(
            self, dims: List[int] = None, input_dim: int = 3,
            conditional_dim: int = 5, his_stp: int = 11
    ):
        super(UnetDiffusionModel, self).__init__()
        if dims is None:
            self.__dims = [64, 128, 256, 512]
        else:
            self.__dims = dims
        self.his_delt_step = his_stp - 1
        self.input_dim = input_dim
        input_tensor_dim = self.his_delt_step * input_dim + conditional_dim * his_stp + 1
        self.layer1 = LinearLayer(input_tensor_dim, self.__dims[0])
        self.layer2 = LinearLayer(self.__dims[0], self.__dims[1])
        self.layer3 = LinearLayer(self.__dims[1], self.__dims[2])
        self.layer4 = LinearLayer(self.__dims[2], self.__dims[3])
        self.layer5 = LinearLayer(self.__dims[3], 64)
        self.decode4 = Decoder(64, 768, self.__dims[2])
        self.decode3 = Decoder(self.__dims[2], 384, self.__dims[1])
        self.decode2 = Decoder(self.__dims[1], 192, self.__dims[0])
        self.output_layer = nn.Sequential(
            nn.Linear(
                self.__dims[0], self.his_delt_step * input_dim,
                bias=True, dtype=torch.float32
            ),
            nn.Tanh()
        )

    def forward(self, perturbed_x, t, predicted_his_traj):
        # batch, obs_num, 10, 5
        batch_size = perturbed_x.shape[0]
        obs_num = perturbed_x.shape[1]
        input_tensor = torch.flatten(perturbed_x, start_dim=2)
        his_traj = torch.flatten(predicted_his_traj, start_dim=2)
        t = t.view(-1, 1, 1).repeat((1, obs_num, 1))
        # batch, obs_num, 50 + 50 + 1
        input_tensor = torch.cat([input_tensor, his_traj, t], dim=-1).to(torch.float32)
        # batch, 64
        e1 = self.layer1(input_tensor)
        e2 = self.layer2(e1)
        e3 = self.layer3(e2)
        e4 = self.layer4(e3)
        f = self.layer5(e4)
        d4 = self.decode4(f, e4)
        d3 = self.decode3(d4, e3)
        d2 = self.decode2(d3, e2)
        out = self.output_layer(d2)
        return out.view(batch_size, obs_num, self.his_delt_step, self.input_dim)


class DitDiffusionModel(nn.Module):
    def __init__(
            self, input_dim: int = 3, conditional_dim: int = 5,
            his_stp: int = 11, num_dit_blocks: int = 4
    ):
        super(DitDiffusionModel, self).__init__()
        self.his_delt_step = his_stp - 1
        self.input_dim = (self.his_delt_step * input_dim) + 1
        self.conditional_dim = his_stp * conditional_dim
        self.dit_blocks = nn.ModuleList([])
        for _ in range(num_dit_blocks):
            self.dit_blocks.append(TransformerCrossAttention(self.input_dim, self.conditional_dim))
        self.linear_output = nn.Sequential(
            nn.Linear(self.input_dim, self.input_dim * 2),
            nn.LayerNorm(self.input_dim * 2),
            nn.ReLU(inplace=True),
            nn.Linear(self.input_dim * 2, self.input_dim),
            nn.LayerNorm(self.input_dim),
            nn.ReLU(inplace=True),
            nn.Linear(self.input_dim, self.his_delt_step * input_dim),
            nn.Tanh()
        )

    def forward(self, perturbed_x, t, predicted_his_traj):
        batch_size = perturbed_x.shape[0]
        obs_num = perturbed_x.shape[1]
        # batch, pred_obs, his_stp * 3
        input_tensor = torch.flatten(perturbed_x, start_dim=2)
        # batch, 15, 1
        t = t.view(-1, 1, 1).repeat(1, obs_num, 1)
        # batch, pred_obs, his_stp * 5
        predicted_his_traj = torch.flatten(predicted_his_traj, start_dim=2)
        # batch, pred_obs, his_stp * 3 + 1
        input_tensor = torch.cat([input_tensor, t], dim=-1)
        for dit_block in self.dit_blocks:
            input_tensor = dit_block(input_tensor, predicted_his_traj)
        noise_output = self.linear_output(input_tensor)
        return noise_output.view(batch_size, obs_num, self.his_delt_step, -1)


class GaussianDiffusion(nn.Module):
    def __init__(
            self, input_dim: int = 5, conditional_dim: int = 5,
            his_stp: int = 11, betas: np.ndarray = None,
            loss_type: str = "l2", num_dit_blocks: int = 4,
            diffusion_type: str = "none"
    ):
        super(GaussianDiffusion, self).__init__()
        if betas is None:
            betas = []
        # l1或者l2损失
        if loss_type not in ["l1", "l2"]:
            raise ValueError(f"get unknown loss type: {loss_type}")
        if diffusion_type not in ["dit", "unet", "none"]:
            raise ValueError(f"get unknown diffusion type: {diffusion_type}")

        self.loss_type = loss_type
        self.diffusion_type = diffusion_type
        self.num_time_steps = len(betas)

        alphas = 1.0 - betas
        alphas_cum_prod = np.cumprod(alphas)
        # 转换成torch.tensor来处理
        to_torch = partial(torch.tensor, dtype=torch.float32)

        # betas             [0.0001, 0.00011992, 0.00013984 ... , 0.02]
        self.register_buffer("betas", to_torch(betas))
        # alphas            [0.9999, 0.99988008, 0.99986016 ... , 0.98]
        self.register_buffer("alphas", to_torch(alphas))
        # alphas_cum_prod    [9.99900000e-01, 9.99780092e-01, 9.99640283e-01 ... , 4.03582977e-05]
        self.register_buffer("alphas_cum_prod", to_torch(alphas_cum_prod))
        # sqrt(alphas_cum_prod)
        self.register_buffer("sqrt_alphas_cum_prod", to_torch(np.sqrt(alphas_cum_prod)))
        # sqrt(1 - alphas_cum_prod)
        self.register_buffer("sqrt_one_minus_alphas_cum_prod", to_torch(np.sqrt(1 - alphas_cum_prod)))
        # sqrt(1 / alphas)
        self.register_buffer("reciprocal_sqrt_alphas", to_torch(np.sqrt(1 / alphas)))
        self.register_buffer("sigma", to_torch(np.sqrt(betas)))
        alphas_cum_prod_prev = np.append(1, alphas_cum_prod[:-1])
        # self.register_buffer("remove_noise_coeff", to_torch(betas / np.sqrt(1 - alphas_cum_prod)))
        self.register_buffer("remove_noise_coeff",
                             to_torch(betas * (1 - alphas_cum_prod_prev / np.sqrt(1 - alphas_cum_prod))))
        # 初始化model
        self.his_delt_step = his_stp - 1
        if self.diffusion_type == "dit":
            self.diffusion_model = DitDiffusionModel(
                input_dim=input_dim,
                conditional_dim=conditional_dim,
                num_dit_blocks=num_dit_blocks
            )
        elif self.diffusion_type == "unet":
            self.diffusion_model = UnetDiffusionModel(
                input_dim=input_dim,
                conditional_dim=conditional_dim,
            )
        else:
            self.diffusion_model = None
        self.input_dim = input_dim
        self.norm_output = nn.BatchNorm2d(5)

    def remove_noise(self, noise, t_batch, predicted_his_traj):
        model_output = self.diffusion_model(noise, t_batch, predicted_his_traj)
        return (
                (noise - extract(self.remove_noise_coeff, t_batch, noise.shape) * model_output) *
                extract(self.reciprocal_sqrt_alphas, t_batch, noise.shape)
        )

    def sample(self, noise, predicted_his_traj):
        if self.diffusion_type != "none":
            batch_size = predicted_his_traj.shape[0]
            device = predicted_his_traj.device
            for t in range(self.num_time_steps - 1, -1, -1):
                t_batch = torch.tensor([t], device=device).repeat(batch_size)
                noise = self.remove_noise(noise, t_batch, predicted_his_traj)
                if t > 0:
                    noise += extract(self.sigma, t_batch, noise.shape) * torch.randn_like(noise)
            noise = torch.transpose(noise, 1, -1)
            noise = self.norm_output(noise)
            noise = torch.transpose(noise, 1, -1)
        return noise

    def perturb_x(self, future_traj, t, noise):
        return (
                extract(self.sqrt_alphas_cum_prod, t, future_traj.shape) * future_traj +
                extract(self.sqrt_one_minus_alphas_cum_prod, t, future_traj.shape) * noise
        )

    def get_losses(self, predicted_his_traj_delt, predicted_his_traj, predicted_traj_mask, t):
        if self.diffusion_type != "none":
            noise = torch.randn_like(predicted_his_traj_delt)
            perturbed_x = self.perturb_x(predicted_his_traj_delt, t, noise)
            estimated_noise = self.diffusion_model(perturbed_x, t, predicted_his_traj)
            batch_size, obs_num = perturbed_x.shape[0], perturbed_x.shape[1]
            # diffusion_loss
            diffusion_loss = F.mse_loss(estimated_noise, noise, reduction="none")
            diffusion_loss_mask = (predicted_traj_mask.view(batch_size, obs_num, 1, 1)
                                   .repeat(1, 1, self.his_delt_step, self.input_dim))
            diffusion_loss = torch.sum(diffusion_loss * diffusion_loss_mask) / torch.sum(diffusion_loss_mask)
            return diffusion_loss
        else:
            return torch.tensor(0).to(torch.float32)

    def forward(self, data: dict):
        # batch, pred_obs(8), his_step, 3
        # batch, pred_obs(8), his_step, 5
        predicted_his_traj_delt = data['predicted_his_traj_delt']
        predicted_his_traj = data['predicted_his_traj']
        predicted_traj_mask = data['predicted_traj_mask']
        batch_size = predicted_his_traj_delt.shape[0]
        device = predicted_his_traj.device
        t = torch.randint(0, self.num_time_steps, (batch_size,), device=device)
        return self.get_losses(predicted_his_traj_delt, predicted_his_traj, predicted_traj_mask, t)
