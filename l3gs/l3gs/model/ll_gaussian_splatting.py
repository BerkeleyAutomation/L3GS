# ruff: noqa: E741
# Copyright 2022 the Regents of the University of California, Nerfstudio Team and contributors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
NeRF implementation that combines many recent advancements.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Type, Union

import numpy as np
import torch
from gsplat.cuda_legacy._torch_impl import quat_to_rotmat

try:
    from gsplat.rendering import rasterization
except ImportError:
    print("Please install gsplat>=1.0.0")
from gsplat.cuda_legacy._wrapper import num_sh_bases
from pytorch_msssim import SSIM
from torch.nn import Parameter
from typing_extensions import Literal

from nerfstudio.cameras.camera_optimizers import CameraOptimizer, CameraOptimizerConfig
from nerfstudio.cameras.cameras import Cameras
from nerfstudio.data.scene_box import OrientedBox
from nerfstudio.engine.callbacks import TrainingCallback, TrainingCallbackAttributes, TrainingCallbackLocation
from nerfstudio.engine.optimizers import Optimizers

# need following import for background color override
from nerfstudio.model_components import renderers
from nerfstudio.models.base_model import Model, ModelConfig
from nerfstudio.utils.colors import get_color
from nerfstudio.utils.rich_utils import CONSOLE

# L3GS imports
from nerfstudio.models.splatfacto import SplatfactoModelConfig, SplatfactoModel
from nerfstudio.viewer.viewer_elements import ViewerButton, ViewerSlider, ViewerControl, ViewerVec3
from l3gs.fields.gaussian_lerf_field import GaussianLERFField
from l3gs.encoders.image_encoder import BaseImageEncoderConfig, BaseImageEncoder
from l3gs.field_components.gaussian_lerf_fieldheadnames import GaussianLERFFieldHeadNames
from nerfstudio.viewer.viewer import VISER_NERFSTUDIO_SCALE_RATIO
from nerfstudio.utils.colormaps import apply_colormap
import viser.transforms as vtf


def random_quat_tensor(N):
    """
    Defines a random quaternion tensor of shape (N, 4)
    """
    u = torch.rand(N)
    v = torch.rand(N)
    w = torch.rand(N)
    return torch.stack(
        [
            torch.sqrt(1 - u) * torch.sin(2 * math.pi * v),
            torch.sqrt(1 - u) * torch.cos(2 * math.pi * v),
            torch.sqrt(u) * torch.sin(2 * math.pi * w),
            torch.sqrt(u) * torch.cos(2 * math.pi * w),
        ],
        dim=-1,
    )


def RGB2SH(rgb):
    """
    Converts from RGB values [0,1] to the 0th spherical harmonic coefficient
    """
    C0 = 0.28209479177387814
    return (rgb - 0.5) / C0


def SH2RGB(sh):
    """
    Converts from the 0th spherical harmonic coefficient to RGB values [0,1]
    """
    C0 = 0.28209479177387814
    return sh * C0 + 0.5


def resize_image(image: torch.Tensor, d: int):
    """
    Downscale images using the same 'area' method in opencv

    :param image shape [H, W, C]
    :param d downscale factor (must be 2, 4, 8, etc.)

    return downscaled image in shape [H//d, W//d, C]
    """
    import torch.nn.functional as tf

    image = image.to(torch.float32)
    weight = (1.0 / (d * d)) * torch.ones((1, 1, d, d), dtype=torch.float32, device=image.device)
    return tf.conv2d(image.permute(2, 0, 1)[:, None, ...], weight, stride=d).squeeze(1).permute(1, 2, 0)


@torch.compile()
def get_viewmat(optimized_camera_to_world):
    """
    function that converts c2w to gsplat world2camera matrix, using compile for some speed
    """
    R = optimized_camera_to_world[:, :3, :3]  # 3 x 3
    T = optimized_camera_to_world[:, :3, 3:4]  # 3 x 1
    # flip the z and y axes to align with gsplat conventions
    R = R * torch.tensor([[[1, -1, -1]]], device=R.device, dtype=R.dtype)
    # analytic matrix inverse to get world2camera matrix
    R_inv = R.transpose(1, 2)
    T_inv = -torch.bmm(R_inv, T)
    viewmat = torch.zeros(R.shape[0], 4, 4, device=R.device, dtype=R.dtype)
    viewmat[:, 3, 3] = 1.0  # homogenous
    viewmat[:, :3, :3] = R_inv
    viewmat[:, :3, 3:4] = T_inv
    return viewmat



@dataclass
class LLGaussianSplattingModelConfig(SplatfactoModelConfig):
    """Gaussian Splatting Model Config"""

    _target: Type = field(default_factory=lambda: LLGaussianSplattingModel)
    warmup_length: int = 500
    """period of steps where refinement is turned off"""
    refine_every: int = 100
    """period of steps where gaussians are culled and densified"""
    resolution_schedule: int = 3000
    """training starts at 1/d resolution, every n steps this is doubled"""
    background_color: Literal["random", "black", "white"] = "random"
    """Whether to randomize the background color."""
    num_downscales: int = 2
    """at the beginning, resolution is 1/2^d, where d is this number"""
    cull_alpha_thresh: float = 0.1
    """threshold of opacity for culling gaussians. One can set it to a lower value (e.g. 0.005) for higher quality."""
    cull_scale_thresh: float = 0.8
    """threshold of scale for culling huge gaussians"""
    continue_cull_post_densification: bool = True
    """If True, continue to cull gaussians post refinement"""
    reset_alpha_every: int = 30
    """Every this many refinement steps, reset the alpha"""
    densify_grad_thresh: float = 0.0008
    """threshold of positional gradient norm for densifying gaussians"""
    densify_size_thresh: float = 0.01
    """below this size, gaussians are *duplicated*, otherwise split"""
    n_split_samples: int = 2
    """number of samples to split gaussians into"""
    sh_degree_interval: int = 1000
    """every n intervals turn on another sh degree"""
    split_screen_size: float = 0.05
    """if a gaussian is more than this percent of screen space, split it"""
    stop_screen_size_at: int = 4000
    """stop culling/splitting at this step WRT screen size of gaussians"""
    random_init: bool = False
    """whether to initialize the positions uniformly randomly (not SFM points)"""
    num_random: int = 15
    """Number of gaussians to initialize if random init is used"""
    random_scale: float = 100.0
    "Size of the cube to initialize random gaussians within"
    init_opacity: float = 0.2
    """Initial opacity of deprojected gaussians"""
    ssim_lambda: float = 0.2
    """weight of ssim loss"""
    stop_split_at: int = 50000
    """stop splitting at this step"""
    sh_degree: int = 3
    """maximum degree of spherical harmonics to use"""
    clip_loss_weight: float = 0.1
    """weight of clip loss"""
    use_scale_regularization: bool = False
    """If enabled, a scale regularization introduced in PhysGauss (https://xpandora.github.io/PhysGaussian/) is used for reducing huge spikey gaussians."""
    max_gauss_ratio: float = 10.0
    """threshold of ratio of gaussian max to min scale before applying regularization
    loss from the PhysGaussian paper
    """
    output_depth_during_training: bool = True
    """If True, output depth during training. Otherwise, only output depth during evaluation."""
    rasterize_mode: Literal["classic", "antialiased"] = "classic"
    """
    Classic mode of rendering will use the EWA volume splatting with a [0.3, 0.3] screen space blurring kernel. This
    approach is however not suitable to render tiny gaussians at higher or lower resolution than the captured, which
    results "aliasing-like" artifacts. The antialiased mode overcomes this limitation by calculating compensation factors
    and apply them to the opacities of gaussians to preserve the total integrated density of splats.

    However, PLY exported with antialiased rasterize mode is not compatible with classic mode. Thus many web viewers that
    were implemented for classic mode can not render antialiased mode PLY properly without modifications.
    """
    camera_optimizer: CameraOptimizerConfig = field(default_factory=lambda: CameraOptimizerConfig(mode="off"))
    """Config of the camera optimizer to use"""


class LLGaussianSplattingModel(SplatfactoModel):
    """Nerfstudio's implementation of Gaussian Splatting

    Args:
        config: Splatfacto configuration to instantiate model
    """

    config: LLGaussianSplattingModel

    def __init__(
        self,
        *args,
        seed_points: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,
        **kwargs,
    ):
        self.seed_points = seed_points
        super().__init__(*args, **kwargs)
        self.deprojected_new = []
        self.colors_new = []
        self.postBA = False
        self.localized_query = None

    def populate_modules(self):
        if self.seed_points is not None and not self.config.random_init:
            means = torch.nn.Parameter(self.seed_points[0])  # (Location, Color)
        else:
            means = torch.nn.Parameter((torch.rand((self.config.num_random, 3)) - 0.5) * self.config.random_scale)
        self.xys_grad_norm = None
        distances, _ = self.k_nearest_sklearn(means.data, 3)
        distances = torch.from_numpy(distances)
        # find the average of the three nearest neighbors for each point and use that as the scale
        avg_dist = distances.mean(dim=-1, keepdim=True)/5.0
        scales = torch.nn.Parameter(torch.log(avg_dist.repeat(1, 3)))
        num_points = means.shape[0]
        quats = torch.nn.Parameter(random_quat_tensor(num_points))
        dim_sh = num_sh_bases(self.config.sh_degree)

        self.gaussian_lerf_field = GaussianLERFField()
        self.datamanager = self.kwargs["datamanager"]
        self.image_encoder: BaseImageEncoder = self.kwargs["image_encoder"]

        if (
            self.seed_points is not None
            and not self.config.random_init
            # We can have colors without points.
            and self.seed_points[1].shape[0] > 0
        ):
            shs = torch.zeros((self.seed_points[1].shape[0], dim_sh, 3)).float().cuda()
            if self.config.sh_degree > 0:
                shs[:, 0, :3] = RGB2SH(self.seed_points[1] / 255)
                shs[:, 1:, 3:] = 0.0
            else:
                CONSOLE.log("use color only optimization with sigmoid activation")
                shs[:, 0, :3] = torch.logit(self.seed_points[1] / 255, eps=1e-10)
            features_dc = torch.nn.Parameter(shs[:, 0, :])
            features_rest = torch.nn.Parameter(shs[:, 1:, :])
        else:
            features_dc = torch.nn.Parameter(torch.rand(num_points, 3))
            features_rest = torch.nn.Parameter(torch.zeros((num_points, dim_sh - 1, 3)))

        opacities = torch.nn.Parameter(torch.logit(0.1 * torch.ones(num_points, 1)))
        self.gauss_params = torch.nn.ParameterDict(
            {
                "means": means,
                "scales": scales,
                "quats": quats,
                "features_dc": features_dc,
                "features_rest": features_rest,
                "opacities": opacities,
            }
        )

        self.camera_optimizer: CameraOptimizer = self.config.camera_optimizer.setup(
            num_cameras=self.num_train_data, device="cpu"
        )

        # metrics
        from torchmetrics.image import PeakSignalNoiseRatio
        from torchmetrics.image.lpip import LearnedPerceptualImagePatchSimilarity

        self.psnr = PeakSignalNoiseRatio(data_range=1.0)
        self.ssim = SSIM(data_range=1.0, size_average=True, channel=3)
        self.lpips = LearnedPerceptualImagePatchSimilarity(normalize=True)
        self.step = 0

        self.crop_box: Optional[OrientedBox] = None
        if self.config.background_color == "random":
            self.background_color = torch.tensor(
                [0.1490, 0.1647, 0.2157]
            )  # This color is the same as the default background color in Viser. This would only affect the background color when rendering.
        else:
            self.background_color = get_color(self.config.background_color)

        #l3gs init
        self.steps_since_add = 0
        
        self.viewer_control = ViewerControl()
        self.viser_scale_ratio = 0.1
        self.frame_on_word = ViewerButton("Localize Query", cb_hook=self.localize_query_cb)
        self.relevancy_thresh = ViewerSlider("Relevancy Thresh", 0.0, 0, 1.0, 0.01)


    @property
    def colors(self):
        if self.config.sh_degree > 0:
            return SH2RGB(self.features_dc)
        else:
            return torch.sigmoid(self.features_dc)

    @property
    def shs_0(self):
        return self.features_dc

    @property
    def shs_rest(self):
        return self.features_rest

    @property
    def num_points(self):
        return self.means.shape[0]

    @property
    def means(self):
        return self.gauss_params["means"]

    @property
    def scales(self):
        return self.gauss_params["scales"]

    @property
    def quats(self):
        return self.gauss_params["quats"]

    @property
    def features_dc(self):
        return self.gauss_params["features_dc"]

    @property
    def features_rest(self):
        return self.gauss_params["features_rest"]

    @property
    def opacities(self):
        return self.gauss_params["opacities"]

    def load_state_dict(self, dict, **kwargs):  # type: ignore
        # resize the parameters to match the new number of points
        self.step = 30000
        if "means" in dict:
            # For backwards compatibility, we remap the names of parameters from
            # means->gauss_params.means since old checkpoints have that format
            for p in ["means", "scales", "quats", "features_dc", "features_rest", "opacities"]:
                dict[f"gauss_params.{p}"] = dict[p]
        newp = dict["gauss_params.means"].shape[0]
        for name, param in self.gauss_params.items():
            old_shape = param.shape
            new_shape = (newp,) + old_shape[1:]
            self.gauss_params[name] = torch.nn.Parameter(torch.zeros(new_shape, device=self.device))
        super().load_state_dict(dict, **kwargs)

    def k_nearest_sklearn(self, x: torch.Tensor, k: int, include_self: bool = False):
        """
        Find k-nearest neighbors using sklearn's NearestNeighbors.
        x: The data tensor of shape [num_samples, num_features]
        k: The number of neighbors to retrieve
        """
        # Convert tensor to numpy array
        x_np = x.cpu().numpy()

        # Build the nearest neighbors model
        from sklearn.neighbors import NearestNeighbors

        nn_model = NearestNeighbors(n_neighbors=k + 1, algorithm="auto", metric="euclidean").fit(x_np)

        # Find the k-nearest neighbors
        distances, indices = nn_model.kneighbors(x_np)

        if include_self:
            return distances.astype(np.float32), indices
        else:
            return distances[:, 1:].astype(np.float32), indices[:, 1:].astype(np.float32)
        
    def add_new_params_to_optimizer(self, optimizer, new_param_groups):
        """
        Adds new parameters to the optimizer, initializing necessary states.

        Args:
            optimizer (torch.optim.Optimizer): The existing optimizer.
            new_param_groups (dict): A dictionary of new parameters to add, categorized by group.
        """
        num_new = new_param_groups[0].shape[0]
        
        param = optimizer.param_groups[0]["params"][0]

        param_state = optimizer.state[param]
        

        repeat_dims = (num_new,) + tuple(1 for _ in range(param_state["exp_avg"].dim() - 1))

        
        param_state["exp_avg"] = torch.cat(
            [param_state["exp_avg"], torch.ones_like(param_state["exp_avg"][-1]).repeat(*repeat_dims) * 0.4],
            dim=0,
        )
        param_state["exp_avg_sq"] = torch.cat(
            [
                param_state["exp_avg_sq"],
                torch.ones_like(param_state["exp_avg_sq"][-1]).repeat(*repeat_dims) * 0.4,
            ],
            dim=0,
        )

        del optimizer.state[param]
        optimizer.state[new_param_groups[0]] = param_state

        optimizer.param_groups[0]["params"] = new_param_groups
        del param

    def add_deprojected_means(self, deprojected, colors, optimizers: Optimizers, step):
        if len(deprojected) > 0:
            with torch.no_grad():
                # import pdb; pdb.set_trace()
                # deprojected = torch.stack(deprojected, dim=0).to(self.device)
                # colors = torch.stack(colors, dim=0).to(self.device)
                deprojected = deprojected[0]
                colors = colors[0]
                numpts = len(deprojected)
                avg_dist = torch.ones_like(deprojected.mean(dim=-1).unsqueeze(-1)) * 0.02 #* 0.01

                # if self.clrs == None:
                #     self.clrs = torch.nn.Parameter(torch.cat([self.means.detach(), colors], dim=0))
                # else:
                #     self.clrs = torch.nn.Parameter(torch.cat([self.clrs.detach(), colors], dim=0))

                dim_sh = num_sh_bases(self.config.sh_degree)
                if colors.max() > 1.0:
                    colors = colors / 255
                    assert colors.max() <= 1.0
                
                shs = torch.zeros((colors.shape[0], dim_sh, 3)).float().cuda()
                if self.config.sh_degree > 0:
                    shs[:, 0, :3] = RGB2SH(colors)
                    shs[:, 1:, 3:] = 0.0
                else:
                    CONSOLE.log("use color only optimization with sigmoid activation")
                    shs[:, 0, :3] = torch.logit(colors, eps=1e-10)

                self.gauss_params['means'] = torch.nn.Parameter(torch.cat([self.gauss_params['means'].detach(), deprojected], dim=0))
                self.gauss_params['scales'] = torch.nn.Parameter(torch.cat([self.gauss_params['scales'].detach(), torch.log(avg_dist.repeat(1, 3)).float().cuda()], dim=0))
                self.gauss_params['quats'] = torch.nn.Parameter(torch.cat([self.gauss_params['quats'].detach(), random_quat_tensor(numpts).float().cuda()]))
                self.gauss_params['features_dc'] = torch.nn.Parameter(torch.cat([self.gauss_params['features_dc'].detach(), shs[:, 0, :].to(self.device)]))
                self.gauss_params['features_rest'] = torch.nn.Parameter(torch.cat([self.gauss_params['features_rest'].detach(), shs[:, 1:, :].to(self.device)]))
                self.gauss_params['opacities'] = torch.nn.Parameter(torch.cat([self.gauss_params['opacities'].detach(), torch.logit(self.config.init_opacity * torch.ones(numpts, 1)).to(self.device)], dim=0))
                

                self.xys_grad_norm = None
                self.vis_counts = None
                self.max_2Dsize = None
                
                num_new_points = deprojected.shape[0]
                
                # Adding only the new parameters to the optimizer
                # new_gaussian_params = [new_means, new_scales, new_quats, new_colors_all, new_opacities]
                param_groups = self.get_gaussian_param_groups()
                for group, param in param_groups.items():
                    if group == 'lerf':
                        continue
                    new_param = [param[0][-num_new_points:]]
                    self.add_new_params_to_optimizer(optimizers.optimizers[group], new_param)

                # if self.num_points == self.config.num_random + numpts:
                #     print("removing random init")
                #     with torch.no_grad():
                #         mask = torch.cat(
                #             (
                #             torch.ones(self.config.num_random, device=self.device, dtype=torch.bool),
                #             torch.zeros(self.num_points - self.config.num_random, device=self.device, dtype=torch.bool)
                #             )
                #         )
                #         deleted_mask = self.cull_gaussians(mask, max(0.02, self.config.init_opacity - 0.05))
                #         # import pdb; pdb.set_trace()
                #         self.remove_from_all_optim(optimizers, deleted_mask)
            
            ## Deproject Debug
            # means_freeze = self.means.data.clone().cpu()
            # colors_freeze = self.clrs.data.clone().cpu()
            # self.viewer_control.viser_server.add_point_cloud("deprojected", means_freeze.numpy(force=True) * VISER_NERFSTUDIO_SCALE_RATIO, colors_freeze.numpy(force=True), 0.1)
            # import pdb; pdb.set_trace()

            colors = colors.detach()
            deprojected = deprojected.detach()
            del colors
            del deprojected
            torch.cuda.empty_cache()
            self.deprojected_new.clear()
            self.colors_new.clear()
            self.steps_since_add = 0
            self.postBA = True

    def remove_from_optim(self, optimizer, deleted_mask, new_params):
        """removes the deleted_mask from the optimizer provided"""
        assert len(new_params) == 1
        # assert isinstance(optimizer, torch.optim.Adam), "Only works with Adam"

        param = optimizer.param_groups[0]["params"][0]
        param_state = optimizer.state[param]
        del optimizer.state[param]

        # Modify the state directly without deleting and reassigning.
        if "exp_avg" in param_state:
            param_state["exp_avg"] = param_state["exp_avg"][~deleted_mask]
            param_state["exp_avg_sq"] = param_state["exp_avg_sq"][~deleted_mask]

        # Update the parameter in the optimizer's param group.
        del optimizer.param_groups[0]["params"][0]
        del optimizer.param_groups[0]["params"]
        optimizer.param_groups[0]["params"] = new_params
        optimizer.state[new_params[0]] = param_state

    def remove_from_all_optim(self, optimizers, deleted_mask):
        param_groups = self.get_gaussian_param_groups()
        for group, param in param_groups.items():
            if group == 'lerf':
                continue
            self.remove_from_optim(optimizers.optimizers[group], deleted_mask, param)
        torch.cuda.empty_cache()

    def dup_in_optim(self, optimizer, dup_mask, new_params, n=2):
        """adds the parameters to the optimizer"""
        param = optimizer.param_groups[0]["params"][0]
        param_state = optimizer.state[param]
        if "exp_avg" in param_state:
            repeat_dims = (n,) + tuple(1 for _ in range(param_state["exp_avg"].dim() - 1))
            param_state["exp_avg"] = torch.cat(
                [
                    param_state["exp_avg"],
                    torch.zeros_like(param_state["exp_avg"][dup_mask.squeeze()]).repeat(*repeat_dims),
                ],
                dim=0,
            )
            param_state["exp_avg_sq"] = torch.cat(
                [
                    param_state["exp_avg_sq"],
                    torch.zeros_like(param_state["exp_avg_sq"][dup_mask.squeeze()]).repeat(*repeat_dims),
                ],
                dim=0,
            )
        del optimizer.state[param]
        optimizer.state[new_params[0]] = param_state
        optimizer.param_groups[0]["params"] = new_params
        del param

    def dup_in_all_optim(self, optimizers, dup_mask, n):
        param_groups = self.get_gaussian_param_groups()
        for group, param in param_groups.items():
            if group == 'lerf':
                continue
            self.dup_in_optim(optimizers.optimizers[group], dup_mask, param, n)

    def after_train(self, step: int):
        assert step == self.step
        # to save some training time, we no longer need to update those stats post refinement
        if self.step >= self.config.stop_split_at:
            return
        with torch.no_grad():
            # keep track of a moving average of grad norms
            visible_mask = (self.radii > 0).flatten()
            grads = self.xys.absgrad[0][visible_mask].norm(dim=-1)  # type: ignore
            # print(f"grad norm min {grads.min().item()} max {grads.max().item()} mean {grads.mean().item()} size {grads.shape}")
            if self.xys_grad_norm is None:
                self.xys_grad_norm = torch.zeros(self.num_points, device=self.device, dtype=torch.float32)
                self.vis_counts = torch.ones(self.num_points, device=self.device, dtype=torch.float32)
            assert self.vis_counts is not None
            self.vis_counts[visible_mask] += 1
            self.xys_grad_norm[visible_mask] += grads

    def set_crop(self, crop_box: Optional[OrientedBox]):
        self.crop_box = crop_box

    def set_background(self, background_color: torch.Tensor):
        assert background_color.shape == (3,)
        self.background_color = background_color

    def refinement_after(self, optimizers: Optimizers, step):
        assert step == self.step
        if self.step <= self.config.warmup_length:
            return
        deleted_mask = None
        with torch.no_grad():
            # Offset all the opacity reset logic by refine_every so that we don't
            # save checkpoints right when the opacity is reset (saves every 2k)
            # then cull
            # only split/cull if we've seen every image since opacity reset
            reset_interval = self.config.reset_alpha_every * self.config.refine_every
            do_densification = (
                self.step < self.config.stop_split_at
                and self.step % reset_interval > self.num_train_data + self.config.refine_every
            )
            if do_densification:
                # then we densify
                assert self.xys_grad_norm is not None and self.vis_counts is not None
                avg_grad_norm = (self.xys_grad_norm / self.vis_counts) * 0.5 * max(self.last_size[0], self.last_size[1])
                high_grads = (avg_grad_norm > self.config.densify_grad_thresh).squeeze()
                splits = (self.scales.exp().max(dim=-1).values > self.config.densify_size_thresh).squeeze()
                splits &= high_grads
                nsamps = self.config.n_split_samples
                split_params = self.split_gaussians(splits, nsamps)

                dups = (self.scales.exp().max(dim=-1).values <= self.config.densify_size_thresh).squeeze()
                dups &= high_grads
                dup_params = self.dup_gaussians(dups)
                for name, param in self.gauss_params.items():
                    self.gauss_params[name] = torch.nn.Parameter(
                        torch.cat([param.detach(), split_params[name], dup_params[name]], dim=0)
                    )

                split_idcs = torch.where(splits)[0]
                self.dup_in_all_optim(optimizers, split_idcs, nsamps)

                dup_idcs = torch.where(dups)[0]
                self.dup_in_all_optim(optimizers, dup_idcs, 1)

                # After a guassian is split into two new gaussians, the original one should also be pruned.
                splits_mask = torch.cat(
                    (
                        splits,
                        torch.zeros(
                            nsamps * splits.sum() + dups.sum(),
                            device=self.device,
                            dtype=torch.bool,
                        ),
                    )
                )
                if self.steps_since_add >= 5500 and self.postBA and self.steps_since_add < 10000:
                    deleted_mask = self.cull_gaussians(splits_mask)
            elif self.step >= self.config.stop_split_at and self.config.continue_cull_post_densification:
                if self.steps_since_add >= 5500 and self.postBA and self.steps_since_add < 10000:
                    deleted_mask = self.cull_gaussians()
            else:
                # if we donot allow culling post refinement, no more gaussians will be pruned.
                deleted_mask = None

            if deleted_mask is not None:
                self.remove_from_all_optim(optimizers, deleted_mask)

            if self.step < self.config.stop_split_at and self.step % reset_interval == self.config.refine_every:
                # Reset value is set to be twice of the cull_alpha_thresh
                reset_value = self.config.cull_alpha_thresh * 2.0
                self.opacities.data = torch.clamp(
                    self.opacities.data,
                    max=torch.logit(torch.tensor(reset_value, device=self.device)).item(),
                )
                # reset the exp of optimizer
                optim = optimizers.optimizers["opacities"]
                param = optim.param_groups[0]["params"][0]
                param_state = optim.state[param]
                param_state["exp_avg"] = torch.zeros_like(param_state["exp_avg"])
                param_state["exp_avg_sq"] = torch.zeros_like(param_state["exp_avg_sq"])

            self.xys_grad_norm = None
            self.vis_counts = None

    def cull_gaussians(self, extra_cull_mask: Optional[torch.Tensor] = None):
        """
        This function deletes gaussians with under a certain opacity threshold
        extra_cull_mask: a mask indicates extra gaussians to cull besides existing culling criterion
        """
        n_bef = self.num_points
        # cull transparent ones
        culls = (torch.sigmoid(self.opacities) < self.config.cull_alpha_thresh).squeeze()
        below_alpha_count = torch.sum(culls).item()
        toobigs_count = 0
        if extra_cull_mask is not None:
            culls = culls | extra_cull_mask
        if self.step > self.config.refine_every * self.config.reset_alpha_every:
            # cull huge ones
            toobigs = (torch.exp(self.scales).max(dim=-1).values > self.config.cull_scale_thresh).squeeze()
            culls = culls | toobigs
            toobigs_count = torch.sum(toobigs).item()
        for name, param in self.gauss_params.items():
            self.gauss_params[name] = torch.nn.Parameter(param[~culls])

        CONSOLE.log(
            f"Culled {n_bef - self.num_points} gaussians "
            f"({below_alpha_count} below alpha thresh, {toobigs_count} too bigs, {self.num_points} remaining)"
        )

        return culls

    def split_gaussians(self, split_mask, samps):
        """
        This function splits gaussians that are too large
        """
        n_splits = split_mask.sum().item()
        CONSOLE.log(f"Splitting {split_mask.sum().item()/self.num_points} gaussians: {n_splits}/{self.num_points}")
        centered_samples = torch.randn((samps * n_splits, 3), device=self.device)  # Nx3 of axis-aligned scales
        scaled_samples = (
            torch.exp(self.scales[split_mask].repeat(samps, 1)) * centered_samples
        )  # how these scales are rotated
        quats = self.quats[split_mask] / self.quats[split_mask].norm(dim=-1, keepdim=True)  # normalize them first
        rots = quat_to_rotmat(quats.repeat(samps, 1))  # how these scales are rotated
        rotated_samples = torch.bmm(rots, scaled_samples[..., None]).squeeze()
        new_means = rotated_samples + self.means[split_mask].repeat(samps, 1)
        # step 2, sample new colors
        new_features_dc = self.features_dc[split_mask].repeat(samps, 1)
        new_features_rest = self.features_rest[split_mask].repeat(samps, 1, 1)
        # step 3, sample new opacities
        new_opacities = self.opacities[split_mask].repeat(samps, 1)
        # step 4, sample new scales
        size_fac = 1.6
        new_scales = torch.log(torch.exp(self.scales[split_mask]) / size_fac).repeat(samps, 1)
        self.scales[split_mask] = torch.log(torch.exp(self.scales[split_mask]) / size_fac)
        # step 5, sample new quats
        new_quats = self.quats[split_mask].repeat(samps, 1)
        out = {
            "means": new_means,
            "features_dc": new_features_dc,
            "features_rest": new_features_rest,
            "opacities": new_opacities,
            "scales": new_scales,
            "quats": new_quats,
        }
        for name, param in self.gauss_params.items():
            if name not in out:
                out[name] = param[split_mask].repeat(samps, 1)
        return out

    def dup_gaussians(self, dup_mask):
        """
        This function duplicates gaussians that are too small
        """
        n_dups = dup_mask.sum().item()
        CONSOLE.log(f"Duplicating {dup_mask.sum().item()/self.num_points} gaussians: {n_dups}/{self.num_points}")
        new_dups = {}
        for name, param in self.gauss_params.items():
            new_dups[name] = param[dup_mask]
        return new_dups

    def get_training_callbacks(
        self, training_callback_attributes: TrainingCallbackAttributes
    ) -> List[TrainingCallback]:
        cbs = []
        cbs.append(TrainingCallback([TrainingCallbackLocation.BEFORE_TRAIN_ITERATION], self.step_cb))
        # The order of these matters
        cbs.append(
            TrainingCallback(
                [TrainingCallbackLocation.AFTER_TRAIN_ITERATION],
                self.after_train,
            )
        )
        cbs.append(
            TrainingCallback(
                [TrainingCallbackLocation.AFTER_TRAIN_ITERATION],
                self.refinement_after,
                update_every_num_iters=self.config.refine_every,
                args=[training_callback_attributes.optimizers],
            )
        )
        cbs.append(
            TrainingCallback(
                [TrainingCallbackLocation.AFTER_TRAIN_ITERATION],
                self.add_deprojected_means,
                args=[self.deprojected_new, self.colors_new, training_callback_attributes.optimizers],
            )
        )
        return cbs

    def step_cb(self, step):
        self.step = step

    def get_gaussian_param_groups(self) -> Dict[str, List[Parameter]]:
        # Here we explicitly use the means, scales as parameters so that the user can override this function and
        # specify more if they want to add more optimizable params to gaussians.
        gpg = {
            name: [self.gauss_params[name]]
            for name in ["means", "scales", "quats", "features_dc", "features_rest", "opacities"]
        }
        gpg["lerf"] = list(self.gaussian_lerf_field.parameters())

        return gpg

    def get_param_groups(self) -> Dict[str, List[Parameter]]:
        """Obtain the parameter groups for the optimizers

        Returns:
            Mapping of different parameter groups
        """
        gps = self.get_gaussian_param_groups()
        self.camera_optimizer.get_param_groups(param_groups=gps)
        return gps

    def _get_downscale_factor(self):
        # if self.training:
        #     return 2 ** max(
        #         (self.config.num_downscales - self.step // self.config.resolution_schedule),
        #         0,
        #     )
        # else:
        return 1

    def _downscale_if_required(self, image):
        d = self._get_downscale_factor()
        if d > 1:
            return resize_image(image, d)
        return image

    @staticmethod
    def get_empty_outputs(width: int, height: int, background: torch.Tensor) -> Dict[str, Union[torch.Tensor, List]]:
        rgb = background.repeat(height, width, 1)
        depth = background.new_ones(*rgb.shape[:2], 1) * 10
        accumulation = background.new_zeros(*rgb.shape[:2], 1)
        return {"rgb": rgb, "depth": depth, "accumulation": accumulation, "background": background}

    def get_outputs(self, camera: Cameras) -> Dict[str, Union[torch.Tensor, List]]:
        """Takes in a Ray Bundle and returns a dictionary of outputs.

        Args:
            ray_bundle: Input bundle of rays. This raybundle should have all the
            needed information to compute the outputs.

        Returns:
            Outputs of model. (ie. rendered colors)
        """
        if not isinstance(camera, Cameras):
            print("Called get_outputs with not a camera")
            return {}

        outputs = {}
        optimized_camera_to_world = self.camera_optimizer.apply_to_camera(camera)[0, ...]

        # get the background color
        if self.training:
            assert camera.shape[0] == 1, "Only one camera at a time"
            optimized_camera_to_world = self.camera_optimizer.apply_to_camera(camera)

            if self.config.background_color == "random":
                background = torch.rand(3, device=self.device)
            elif self.config.background_color == "white":
                background = torch.ones(3, device=self.device)
            elif self.config.background_color == "black":
                background = torch.zeros(3, device=self.device)
            else:
                background = self.background_color.to(self.device)
        else:
            optimized_camera_to_world = camera.camera_to_worlds

            if renderers.BACKGROUND_COLOR_OVERRIDE is not None:
                background = renderers.BACKGROUND_COLOR_OVERRIDE.to(self.device)
            else:
                background = self.background_color.to(self.device)

        if self.crop_box is not None and not self.training:
            crop_ids = self.crop_box.within(self.means).squeeze()
            if crop_ids.sum() == 0:
                return self.get_empty_outputs(int(camera.width.item()), int(camera.height.item()), background)
        else:
            crop_ids = None
        camera_scale_fac = 1.0 / self._get_downscale_factor()
        viewmat = get_viewmat(optimized_camera_to_world)
        W, H = int(camera.width[0] * camera_scale_fac), int(camera.height[0] * camera_scale_fac)
        self.last_size = (H, W)

        if crop_ids is not None:
            opacities_crop = self.opacities[crop_ids]
            means_crop = self.means[crop_ids]
            features_dc_crop = self.features_dc[crop_ids]
            features_rest_crop = self.features_rest[crop_ids]
            scales_crop = self.scales[crop_ids]
            quats_crop = self.quats[crop_ids]
        else:
            opacities_crop = self.opacities
            means_crop = self.means
            features_dc_crop = self.features_dc
            features_rest_crop = self.features_rest
            scales_crop = self.scales
            quats_crop = self.quats

        colors_crop = torch.cat((features_dc_crop[:, None, :], features_rest_crop), dim=1)
        BLOCK_WIDTH = 16  # this controls the tile size of rasterization, 16 is a good default
        K = camera.get_intrinsics_matrices().cuda()
        K[:, :2, :] *= camera_scale_fac
        # apply the compensation of screen space blurring to gaussians
        if self.config.rasterize_mode not in ["antialiased", "classic"]:
            raise ValueError("Unknown rasterize_mode: %s", self.config.rasterize_mode)

        if self.config.output_depth_during_training or not self.training:
            render_mode = "RGB+ED"
        else:
            render_mode = "RGB"

        if self.config.sh_degree > 0:
            sh_degree_to_use = min(self.step // self.config.sh_degree_interval, self.config.sh_degree)
        else:
            sh_degree_to_use = None

        render, alpha, info = rasterization(
            means=means_crop,
            quats=quats_crop / quats_crop.norm(dim=-1, keepdim=True),
            scales=torch.exp(scales_crop),
            opacities=torch.sigmoid(opacities_crop).squeeze(-1),
            colors=colors_crop,
            viewmats=viewmat,  # [1, 4, 4]
            Ks=K,  # [1, 3, 3]
            width=W,
            height=H,
            tile_size=BLOCK_WIDTH,
            packed=False,
            near_plane=0.01,
            far_plane=1e10,
            render_mode=render_mode,
            sh_degree=sh_degree_to_use,
            sparse_grad=False,
            absgrad=True,
            rasterize_mode=self.config.rasterize_mode,
            # set some threshold to disregrad small gaussians for faster rendering.
            # radius_clip=3.0,
        )
        if self.training and info["means2d"].requires_grad:
            info["means2d"].retain_grad()
        self.xys = info["means2d"]  # [1, N, 2]
        self.radii = info["radii"][0]  # [N]

        alpha = alpha[:, ...]
        rgb = render[:, ..., :3] + (1 - alpha) * background
        rgb = torch.clamp(rgb, 0.0, 1.0)
        outputs["rgb"] = rgb.squeeze(0)
        if render_mode == "RGB+ED":
            depth_im = render[:, ..., 3:4]
            depth_im = torch.where(alpha > 0, depth_im, depth_im.detach().max()).squeeze(0)
        else:
            depth_im = None
        outputs["depth"] = depth_im
        outputs["accumulation"] = alpha.squeeze(0)
        outputs["background"] = background

        if self.datamanager.use_clip:
            if self.step - self.datamanager.lerf_step > 500:
                if camera.metadata is not None:
                    if "clip_downscale_factor" not in camera.metadata:
                        return {"rgb": rgb.squeeze(0), "depth": depth_im, "accumulation": alpha.squeeze(0), "background": background}
                ########################
                # CLIP Relevancy Field #
                ########################
                reset_interval = self.config.reset_alpha_every * self.config.refine_every
                if self.training and self.step>self.config.warmup_length and (self.step % reset_interval > self.num_train_data + self.config.refine_every  or self.step < (self.config.reset_alpha_every * self.config.refine_every)):
                    # with torch.no_grad():
                    clip_hash_encoding = self.gaussian_lerf_field.get_hash(self.means)
                    # downscale_factor = camera.metadata["clip_downscale_factor"]
                    # print("K: ", K)

                    rgb_downscale = self.datamanager.train_dataset._dataparser_outputs.metadata['image_downscale_factor']

                    downscale_factor = camera.metadata["clip_downscale_factor"] / rgb_downscale

                    camera.rescale_output_resolution(1 / downscale_factor)
                    clip_W, clip_H = camera.width.item(), camera.height.item()
                    # print(f"clip_W {clip_W} clip_H {clip_H}")
                    clipK = camera.get_intrinsics_matrices().cuda()
                    # print("clipK: ", clipK)
                    

                    field_output, alpha, info = rasterization(
                        means=means_crop,
                        quats=quats_crop / quats_crop.norm(dim=-1, keepdim=True),
                        scales=torch.exp(scales_crop),
                        opacities=torch.sigmoid(opacities_crop).squeeze(-1),
                        colors=clip_hash_encoding,
                        viewmats=viewmat, # [1, 4, 4]
                        Ks=clipK,  # [1, 3, 3]
                        width=clip_W,
                        height=clip_H,
                        tile_size=BLOCK_WIDTH,
                        packed=False,
                        near_plane=0.01,
                        far_plane=1e10,
                        render_mode="RGB",
                        sparse_grad=False,
                        absgrad=True,
                        rasterize_mode=self.config.rasterize_mode,
                        # set some threshold to disregrad small gaussians for faster rendering.
                        # radius_clip=3.0,
                    )

                    # rescale the camera back to original dimensions
                    camera.rescale_output_resolution(downscale_factor)
                    

                    self.random_pixels = self.datamanager.random_pixels.to(self.device)

                    clip_scale = self.datamanager.curr_scale * torch.ones((self.random_pixels.shape[0],1),device=self.device)
                    clip_scale = clip_scale * clip_H * (depth_im.view(-1, 1)[self.random_pixels] / camera.fy.item())

                    field_output = self.gaussian_lerf_field.get_outputs_from_feature(field_output.view(clip_H*clip_W, -1)[self.random_pixels], clip_scale)

                    clip_output = field_output[GaussianLERFFieldHeadNames.CLIP].to(dtype=torch.float32)

                    outputs["clip"] = clip_output
                    outputs["clip_scale"] = clip_scale

                if not self.training:
                    # N x B x 1; N
                    max_across, self.best_scales = self.get_max_across(means_crop, quats_crop, scales_crop, opacities_crop, viewmat, K, H, W, preset_scales=None)

                    for i in range(len(self.image_encoder.positives)):
                        max_across[i][max_across[i] < self.relevancy_thresh.value] = 0
                        # relevancy_rasterized[relevancy_rasterized < 0.5] = 0
                        outputs[f"relevancy_{i}"] = max_across[i].view(H, W, -1)
                        # outputs[f"relevancy_rasterized_{i}"] = relevancy_rasterized.view(H, W, -1)
                        # outputs[f"best_scales_{i}"] = best_scales[i]
                
        # return {"rgb": rgb.squeeze(0), "depth": depth_im, "accumulation": alpha.squeeze(0), "background": background, "clip": clip_im, "clip_scale": clip}  # type: ignore
        return outputs
    def get_gt_img(self, image: torch.Tensor):
        """Compute groundtruth image with iteration dependent downscale factor for evaluation purpose

        Args:
            image: tensor.Tensor in type uint8 or float32
        """
        if image.dtype == torch.uint8:
            image = image.float() / 255.0
        gt_img = self._downscale_if_required(image)
        return gt_img.to(self.device)

    def composite_with_background(self, image, background) -> torch.Tensor:
        """Composite the ground truth image with a background color when it has an alpha channel.

        Args:
            image: the image to composite
            background: the background color
        """
        if image.shape[2] == 4:
            alpha = image[..., -1].unsqueeze(-1).repeat((1, 1, 3))
            return alpha * image[..., :3] + (1 - alpha) * background
        else:
            return image

    def get_metrics_dict(self, outputs, batch) -> Dict[str, torch.Tensor]:
        """Compute and returns metrics.

        Args:
            outputs: the output to compute loss dict to
            batch: ground truth batch corresponding to outputs
        """
        gt_rgb = self.composite_with_background(self.get_gt_img(batch["image"]), outputs["background"])
        metrics_dict = {}
        predicted_rgb = outputs["rgb"]
        metrics_dict["psnr"] = self.psnr(predicted_rgb, gt_rgb)

        metrics_dict["gaussian_count"] = self.num_points

        self.camera_optimizer.get_metrics_dict(metrics_dict)
        return metrics_dict

    def get_loss_dict(self, outputs, batch, metrics_dict=None) -> Dict[str, torch.Tensor]:
        """Computes and returns the losses dict.

        Args:
            outputs: the output to compute loss dict to
            batch: ground truth batch corresponding to outputs
            metrics_dict: dictionary of metrics, some of which we can use for loss
        """
        gt_img = self.composite_with_background(self.get_gt_img(batch["image"]), outputs["background"])
        pred_img = outputs["rgb"]

        # Set masked part of both ground-truth and rendered image to black.
        # This is a little bit sketchy for the SSIM loss.
        if "mask" in batch:
            # batch["mask"] : [H, W, 1]
            mask = self._downscale_if_required(batch["mask"])
            mask = mask.to(self.device)
            assert mask.shape[:2] == gt_img.shape[:2] == pred_img.shape[:2]
            gt_img = gt_img * mask
            pred_img = pred_img * mask

        Ll1 = torch.abs(gt_img - pred_img).mean()
        simloss = 1 - self.ssim(gt_img.permute(2, 0, 1)[None, ...], pred_img.permute(2, 0, 1)[None, ...])
        if self.config.use_scale_regularization and self.step % 10 == 0:
            scale_exp = torch.exp(self.scales)
            scale_reg = (
                torch.maximum(
                    scale_exp.amax(dim=-1) / scale_exp.amin(dim=-1),
                    torch.tensor(self.config.max_gauss_ratio),
                )
                - self.config.max_gauss_ratio
            )
            scale_reg = 0.1 * scale_reg.mean()
        else:
            scale_reg = torch.tensor(0.0).to(self.device)

        loss_dict = {
            "main_loss": (1 - self.config.ssim_lambda) * Ll1 + self.config.ssim_lambda * simloss,
            "scale_reg": scale_reg,
        }

        if self.training and 'clip' in outputs and 'clip' in batch: 
            unreduced_clip = self.config.clip_loss_weight * torch.nn.functional.huber_loss(
                outputs["clip"], batch["clip"].to(self.device).to(torch.float32), delta=1.25, reduction="none"
            )
            loss_dict["clip_loss"] = unreduced_clip.sum(dim=-1).nanmean()

        if self.training:
            # Add loss from camera optimizer
            self.camera_optimizer.get_loss_dict(loss_dict)

        return loss_dict

    @torch.no_grad()
    def get_outputs_for_camera(self, camera: Cameras, obb_box: Optional[OrientedBox] = None) -> Dict[str, torch.Tensor]:
        """Takes in a camera, generates the raybundle, and computes the output of the model.
        Overridden for a camera-based gaussian model.

        Args:
            camera: generates raybundle
        """
        assert camera is not None, "must provide camera to gaussian model"
        self.set_crop(obb_box)
        outs = self.get_outputs(camera.to(self.device))
        return outs  # type: ignore

    def get_image_metrics_and_images(
        self, outputs: Dict[str, torch.Tensor], batch: Dict[str, torch.Tensor]
    ) -> Tuple[Dict[str, float], Dict[str, torch.Tensor]]:
        """Writes the test image outputs.

        Args:
            image_idx: Index of the image.
            step: Current step.
            batch: Batch of data.
            outputs: Outputs of the model.

        Returns:
            A dictionary of metrics.
        """
        gt_rgb = self.composite_with_background(self.get_gt_img(batch["image"]), outputs["background"])
        predicted_rgb = outputs["rgb"]

        combined_rgb = torch.cat([gt_rgb, predicted_rgb], dim=1)

        # Switch images from [H, W, C] to [1, C, H, W] for metrics computations
        gt_rgb = torch.moveaxis(gt_rgb, -1, 0)[None, ...]
        predicted_rgb = torch.moveaxis(predicted_rgb, -1, 0)[None, ...]

        psnr = self.psnr(gt_rgb, predicted_rgb)
        ssim = self.ssim(gt_rgb, predicted_rgb)
        lpips = self.lpips(gt_rgb, predicted_rgb)

        # all of these metrics will be logged as scalars
        metrics_dict = {"psnr": float(psnr.item()), "ssim": float(ssim)}  # type: ignore
        metrics_dict["lpips"] = float(lpips)

        images_dict = {"img": combined_rgb}

        return metrics_dict, images_dict
    


    def localize_query_cb(self,element):
        with torch.no_grad():
            # clip_feats = self.gaussian_lerf_field.get_outputs_from_feature(self.clip_hash / self.clip_hash.norm(dim=-1,keepdim=True), self.crop_scale.value * torch.ones(self.num_points, 1, device=self.device))[GaussianLERFFieldHeadNames.CLIP].to(dtype=torch.float32)
            # clip_feats = self.gaussian_lerf_field.get_outputs(self.means, self.crop_scale.value * torch.ones(self.num_points, 1, device=self.device))[GaussianLERFFieldHeadNames.CLIP].to(dtype=torch.float32)
            # clip_feats = self.gaussian_lerf_field.get_outputs(self.means, self.best_scales[0].to(self.device) * torch.ones(self.num_points, 1, device=self.device))[GaussianLERFFieldHeadNames.CLIP].to(dtype=torch.float32)

            # Do K nearest neighbors for each point and then avg the clip hash for each point based on the KNN
            # import pdb; pdb.set_trace()
            means_freeze = self.means.data.clone().detach()
            distances, indicies = self.k_nearest_sklearn(means_freeze, 3, True)
            distances = torch.from_numpy(distances).to(self.device)
            indicies = torch.from_numpy(indicies).view(-1)
            weights = torch.sigmoid(self.opacities[indicies].view(-1, 4))
            weights = torch.nn.Softmax(dim=-1)(weights)
            points = means_freeze[indicies]
            # clip_hash_encoding = self.gaussian_lerf_field.get_hash(self.means)
            clip_hash_encoding = self.gaussian_lerf_field.get_hash(points)
            clip_hash_encoding = clip_hash_encoding.view(-1, 4, clip_hash_encoding.shape[1])
            clip_hash_encoding = (clip_hash_encoding * weights.unsqueeze(-1))
            clip_hash_encoding = clip_hash_encoding.sum(dim=1)
            clip_feats = self.gaussian_lerf_field.get_outputs_from_feature(clip_hash_encoding, self.best_scales[0].to(self.device) * torch.ones(self.num_points, 1, device=self.device))[GaussianLERFFieldHeadNames.CLIP].to(dtype=torch.float32)
            relevancy = self.image_encoder.get_relevancy(clip_feats / (clip_feats.norm(dim=-1, keepdim=True)+1e-6), 0).view(self.num_points, -1)
            # color = apply_colormap(relevancy[..., 0:1])
            # self.viewer_control.viser_server.add_point_cloud("relevancy", self.means.numpy(force=True) * 10, color.numpy(force=True), 0.01)

            # Add a slider to debug the relevancy values
            
            # self.crop_ids = (relevancy[..., 0] > self.relevancy_thresh.value)
            
            #Define all crop viewer elements
            # self.crop_points = relevancy[..., 0] > self.relevancy_thresh.value
            # self._crop_center_init = self.means[self.crop_points].mean(dim=0).cpu().numpy()
            self._crop_center_init = means_freeze[relevancy[..., 0].argmax(dim=0).cpu().numpy()].cpu().numpy()
            # self.original_means = self.means.data.clone()
            
            query = self._crop_center_init / self.viser_scale_ratio

            # self.viewer_control.viser_server.add_icosphere(
            # "/query",
            # radius = 4, 
            # color = (1.0, 0.0, 0.0),
            # position=(query[0], query[1], query[2]),
            # )
            # self.viewer_control.viser_server.add_frame(
            # "/query",
            # axes_length = 4, 
            # axes_radius = 0.025 * 3,
            # wxyz=(1.0, 0.0, 0.0, 0.0),
            # position=(query[0], query[1], query[2]),
            # )
            self.viewer_control.viser_server.add_icosphere(
            "/query",
            radius = self.best_scales[0].item(), 
            color = (1.0, 1.0, 1.0),
            position=(query[0], query[1], query[2]),
            )


            H = self.datamanager.train_dataset._dataparser_outputs.dataparser_transform
            row = torch.tensor([[0,0,0,1]],dtype=torch.float32,device=H.device)

            inv_H = torch.cat([torch.cat([H[:3, :3].transpose(1, 0), -H[:3, 3:]], dim=1), row], dim=0)
            query_world = inv_H @ torch.tensor([query[0], query[1], query[2], 1],dtype=torch.float32,device=H.device)
            print("Query Location:", query_world / VISER_NERFSTUDIO_SCALE_RATIO)
            print("Best Scale:", self.best_scales[0].item())

            self.localized_query = query_world[:3].cpu().numpy() / VISER_NERFSTUDIO_SCALE_RATIO
            
            # self._crop_handle = self.viewer_control.viser_server.add_transform_controls("Crop Points", depth_test=False, line_width=4.0)
            # world_center = tuple(p / self.viser_scale_ratio for p in self._crop_center_init)
            # self._crop_handle.position = world_center

            # self._crop_center.value = tuple(p / self.viser_scale_ratio for p in self._crop_center_init)

            # self.viewer_control.viser_server.add_point_cloud("Centroid", self._crop_center_init / self.viser_scale_ratio, np.array([0,0,0]), 0.1)


    def crop_to_word_cb(self,element):
        with torch.no_grad():
            # clip_feats = self.gaussian_lerf_field.get_outputs_from_feature(self.clip_hash / self.clip_hash.norm(dim=-1,keepdim=True), self.crop_scale.value * torch.ones(self.num_points, 1, device=self.device))[GaussianLERFFieldHeadNames.CLIP].to(dtype=torch.float32)
            # clip_feats = self.gaussian_lerf_field.get_outputs(self.means, self.crop_scale.value * torch.ones(self.num_points, 1, device=self.device))[GaussianLERFFieldHeadNames.CLIP].to(dtype=torch.float32)
            # clip_feats = self.gaussian_lerf_field.get_outputs(self.means, self.best_scales[0].to(self.device) * torch.ones(self.num_points, 1, device=self.device))[GaussianLERFFieldHeadNames.CLIP].to(dtype=torch.float32)

            # Do K nearest neighbors for each point and then avg the clip hash for each point based on the KNN
            distances, indicies = self.k_nearest_sklearn(self.means.data, 3, True)
            distances = torch.from_numpy(distances).to(self.device)
            indicies = torch.from_numpy(indicies).to(self.device).view(-1)
            weights = torch.sigmoid(self.opacities[indicies].view(-1, 4))
            weights = torch.nn.Softmax(dim=-1)(weights)
            points = self.means[indicies]
            # clip_hash_encoding = self.gaussian_lerf_field.get_hash(self.means)
            clip_hash_encoding = self.gaussian_lerf_field.get_hash(points)
            clip_hash_encoding = clip_hash_encoding.view(-1, 4, clip_hash_encoding.shape[1])
            clip_hash_encoding = (clip_hash_encoding * weights.unsqueeze(-1))
            clip_hash_encoding = clip_hash_encoding.sum(dim=1)
            clip_feats = self.gaussian_lerf_field.get_outputs_from_feature(clip_hash_encoding, self.best_scales[0].to(self.device) * torch.ones(self.num_points, 1, device=self.device))[GaussianLERFFieldHeadNames.CLIP].to(dtype=torch.float32)
            relevancy = self.image_encoder.get_relevancy(clip_feats / (clip_feats.norm(dim=-1, keepdim=True)+1e-6), 0).view(self.num_points, -1)
            color = apply_colormap(relevancy[..., 0:1])
            self.viewer_control.viser_server.add_point_cloud("relevancy", self.means.numpy(force=True) * 10, color.numpy(force=True), 0.01)

            # Add a slider to debug the relevancy values
            
            # self.crop_ids = (relevancy[..., 0] > self.relevancy_thresh.value)
            
            #Define all crop viewer elements
            self.crop_points = relevancy[..., 0] > self.relevancy_thresh.value
            self._crop_center_init = self.means[self.crop_points].mean(dim=0).cpu().numpy()
            self.original_means = self.means.data.clone()

            self._crop_handle = self.viewer_control.viser_server.add_transform_controls("Crop Points", depth_test=False, line_width=4.0)
            world_center = tuple(p / self.viser_scale_ratio for p in self._crop_center_init)
            self._crop_handle.position = world_center

            @self._crop_handle.on_update
            def _update_crop_handle(han):
                # import pdb; pdb.set_trace()
                if self._crop_center_init is None:
                    return
                # import pdb; pdb.set_trace()
                new_center = np.array(self._crop_handle.position) * self.viser_scale_ratio
                delta = new_center - self._crop_center_init
                displacement = torch.zeros_like(self.means)
                displacement[self.crop_points] = torch.from_numpy(delta).to(self.device).to(self.means.dtype)
                
                curr_to_world = torch.from_numpy(vtf.SE3(np.concatenate((self._crop_handle.wxyz, self._crop_handle.position * self.viser_scale_ratio))).as_matrix()).to(self.device).to(self.means.dtype)
                transform = torch.from_numpy(vtf.SE3(np.concatenate((self._crop_handle.wxyz, (self._crop_handle.position * self.viser_scale_ratio) - self._crop_center_init))).as_matrix()).to(self.device).to(self.means.dtype)

                print(f"transform {transform}")
                transformed_points = self.original_means.clone()
                homogeneous_points = torch.cat((transformed_points[self.crop_points], torch.ones(transformed_points[self.crop_points].shape[0], 1, device=self.device, dtype=self.means.dtype)), dim=1)
                transformed_homogeneous = curr_to_world @ transform @ torch.inverse(curr_to_world) @ homogeneous_points.transpose(0,1)
                transformed_homogeneous = transformed_homogeneous.transpose(0,1)
                transformed_points[self.crop_points] = transformed_homogeneous[:, :3] / transformed_homogeneous[:, 3:4]
                self.means.data = transformed_points

            # self._crop_center.value = tuple(p / self.viser_scale_ratio for p in self._crop_center_init)

            self.viewer_control.viser_server.add_point_cloud("Centroid", self._crop_center_init / self.viser_scale_ratio, np.array([0,0,0]), 0.1)

    def reset_crop_cb(self,element):
        self.crop_ids = None#torch.ones_like(self.means[:,0],dtype=torch.bool)
        self.means.data = self.original_means
        self._crop_center_init = None
        self._crop_handle.visible = False
        
    def get_max_across(self, means_crop, quats_crop, scales_crop, opacities_crop, viewmat, K, H, W, preset_scales=None):
        # probably not a good idea bc it's prob going to be a lot of memory
        n_phrases = len(self.image_encoder.positives)
        n_phrases_maxs = [None for _ in range(n_phrases)]
        n_phrases_sims = [None for _ in range(n_phrases)]
        scales_list = torch.linspace(0.0, 1.5, 30).to(self.device)
        # scales_list = [0.1]
        all_probs = []
        BLOCK_WIDTH = 16

        with torch.no_grad():
            clip_hash_encoding = self.gaussian_lerf_field.get_hash(self.means)
            # print(type(clip_hash_encoding))
            # print(clip_hash_encoding.ndimension())
            # print(clip_hash_encoding.size(1))
            # import pdb; pdb.set_trace()
            field_output, alpha, info = rasterization(
                        means=means_crop,
                        quats=quats_crop / quats_crop.norm(dim=-1, keepdim=True),
                        scales=torch.exp(scales_crop),
                        opacities=torch.sigmoid(opacities_crop).squeeze(-1),
                        colors=clip_hash_encoding,
                        viewmats=viewmat, # [1, 4, 4]
                        Ks=K,  # [1, 3, 3]
                        width=W,
                        height=H,
                        tile_size=BLOCK_WIDTH,
                        packed=False,
                        near_plane=0.01,
                        far_plane=1e10,
                        render_mode="RGB",
                        sparse_grad=False,
                        absgrad=True,
                        rasterize_mode=self.config.rasterize_mode,
                        # set some threshold to disregrad small gaussians for faster rendering.
                        # radius_clip=3.0,
            )
            # clip_output = rasterize_gaussians(
            #                 xys,
            #                 depths,
            #                 radii,
            #                 conics,
            #                 num_tiles_hit,
            #                 # self.gaussian_lerf_field.get_outputs_from_feature(self.clip_hash / self.clip_hash.norm(dim=-1, keepdim=True), scale * torch.ones(h*w, 1, device=self.device))[GaussianLERFFieldHeadNames.CLIP].view(self.num_points, -1).to(dtype=torch.float32),
            #                 # self.clip_hash / self.clip_hash.norm(dim=-1, keepdim=True),
            #                 # clip_hash_encoding / clip_hash_encoding.norm(dim=-1, keepdim=True),
            #                 clip_hash_encoding,
            #                 opacities,
            #                 h,
            #                 w,
            #                 BLOCK_WIDTH,
            #                 # torch.zeros(self.image_encoder.embedding_dim, device=self.device),
            #                 torch.zeros(clip_hash_encoding.shape[1], device=self.device),
            #             )
            # Normalize the clip output
            # clip_output = clip_output / (clip_output.norm(dim=-1, keepdim=True) + 1e-6)
        for i, scale in enumerate(scales_list):
            with torch.no_grad():
                clip_output_im = self.gaussian_lerf_field.get_outputs_from_feature(field_output.view(H*W, -1), scale * torch.ones(H*W, 1, device=self.device))[GaussianLERFFieldHeadNames.CLIP].to(dtype=torch.float32).view(H, W, -1)

            for j in range(n_phrases):
                if preset_scales is None or j == i:
                    # relevancy_rasterized = NDRasterizeGaussians.apply(
                    #         xys,
                    #         depths,
                    #         radii,
                    #         conics,
                    #         num_tiles_hit,
                    #         self.image_encoder.get_relevancy(self.gaussian_lerf_field.get_outputs_from_feature(self.clip_hash / self.clip_hash.norm(dim=-1, keepdim=True), scale * torch.ones(self.num_points, 1, device=self.device))[GaussianLERFFieldHeadNames.CLIP].view(self.num_points, -1).to(dtype=torch.float32), j)[..., 0:1],
                    #         opacities,
                    #         h,
                    #         w,
                    #         torch.zeros(1, device=self.device),
                    #     )
                    
                    probs = self.image_encoder.get_relevancy(clip_output_im.view(-1, self.image_encoder.embedding_dim), j)
                    pos_prob = probs[..., 0:1]
                    all_probs.append((pos_prob.max(), scale))
                    if n_phrases_maxs[j] is None or pos_prob.max() > n_phrases_sims[j].max():
                        n_phrases_maxs[j] = scale
                        n_phrases_sims[j] = pos_prob
        # print(f"Best scales: {n_phrases_maxs}")#, Words: {self.image_encoder.positives}, Scale List: {scales_list}, All probs: {all_probs}")
        # import pdb; pdb.set_trace()
        return torch.stack(n_phrases_sims), torch.Tensor(n_phrases_maxs)#, relevancy_rasterized
