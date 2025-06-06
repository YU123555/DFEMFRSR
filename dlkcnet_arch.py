import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision
import warnings

from basicsr.archs.arch_util import flow_warp
from basicsr.archs.basicvsr_arch import ConvResidualBlocks, ConvResidualBlocksModified
from basicsr.archs.spynet_arch import SpyNet
from basicsr.ops.dcn import ModulatedDeformConvPack
from basicsr.utils.registry import ARCH_REGISTRY
from basicsr.archs.basicvsr_arch import TSAFusion
from .deform_conv_arch import DeformConv2d

class MultiScaleSmallKernelConvAndAttention(nn.Module):
    def __init__(self, in_channels):
        super(MultiScaleSmallKernelConvAndAttention, self).__init__()

        # Multi-scale large kernel deformable convolutions
        self.deform_conv1x1 = DeformConv2d(in_channels, in_channels, kernel_size=1, padding=0)
        self.deform_conv3x3 = DeformConv2d(in_channels, in_channels, kernel_size=3, padding=1)
        self.deform_conv5x5 = DeformConv2d(in_channels, in_channels, kernel_size=5, padding=2)

        # Parallel attention mechanism (unchanged)
        self.att_conv1 = nn.Conv2d(in_channels, in_channels, kernel_size=1)
        self.att_conv2 = nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1)
        self.att_conv3 = nn.Conv2d(in_channels, in_channels, kernel_size=5, padding=2)

        # Activation functions
        self.relu = nn.LeakyReLU(negative_slope=0.1, inplace=True)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        # Multi-scale deformable convolutions
        x1 = self.relu(self.deform_conv1x1(x))  # 1x1 kernel
        x2 = self.relu(self.deform_conv3x3(x))  # 3x3 kernel
        x3 = self.relu(self.deform_conv5x5(x))  # 5x5 kernel

        # Concatenate multi-scale features
        multi_scale_features = x1 + x2 + x3

        # Parallel attention mechanism
        att1 = self.att_conv1(multi_scale_features)
        att2 = self.att_conv2(multi_scale_features)
        att3 = self.att_conv3(multi_scale_features)

        # Apply sigmoid activation for attention weights
        attention_map = self.sigmoid(att1 + att2 + att3)

        # Apply attention to the feature map
        return multi_scale_features * attention_map + multi_scale_features

class MultiScaleLargeKernelConvAndAttention(nn.Module):
    def __init__(self, in_channels):
        super(MultiScaleLargeKernelConvAndAttention, self).__init__()

        # Multi-scale large kernel deformable convolutions
        self.deform_conv7x7 = DeformConv2d(in_channels, in_channels, kernel_size=7, padding=3)
        self.deform_conv11x11 = DeformConv2d(in_channels, in_channels, kernel_size=11, padding=5)
        self.deform_conv13x13 = DeformConv2d(in_channels, in_channels, kernel_size=13, padding=6)

        # Parallel attention mechanism (unchanged)
        self.att_conv1 = nn.Conv2d(in_channels, in_channels, kernel_size=1)
        self.att_conv2 = nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1)
        self.att_conv3 = nn.Conv2d(in_channels, in_channels, kernel_size=5, padding=2)

        # Activation functions
        self.relu = nn.LeakyReLU(negative_slope=0.1, inplace=True)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        # Multi-scale deformable convolutions
        x1 = self.relu(self.deform_conv7x7(x))  # 7x7 kernel
        x2 = self.relu(self.deform_conv11x11(x))  # 11x11 kernel
        x3 = self.relu(self.deform_conv13x13(x))  # 13x13 kernel

        # Ensure all features have the same spatial size before summing them
        # Here, we upsample all feature maps to the size of the largest feature map (x3)
        x1 = F.interpolate(x1, size=x3.shape[2:], mode='bilinear', align_corners=False)
        x2 = F.interpolate(x2, size=x3.shape[2:], mode='bilinear', align_corners=False)

        # Concatenate multi-scale features
        multi_scale_features = x1 + x2 + x3

        # Parallel attention mechanism
        att1 = self.att_conv1(multi_scale_features)
        att2 = self.att_conv2(multi_scale_features)
        att3 = self.att_conv3(multi_scale_features)

        # Apply sigmoid activation for attention weights
        attention_map = self.sigmoid(att1 + att2 + att3)

        # Apply attention to the feature map
        return multi_scale_features * attention_map + multi_scale_features


@ARCH_REGISTRY.register()
class MFRSR(nn.Module):
   """
   Args:
       mid_channels (int, optional): Channel number of the intermediate
           features. Default: 64.
       num_blocks (int, optional): The number of residual blocks in each
           propagation branch. Default: 7.
       max_residue_magnitude (int): The maximum magnitude of the offset
           residue (Eq. 6 in paper). Default: 10.
       is_low_res_input (bool, optional): Whether the input is low-resolution
           or not. If False, the output resolution is equal to the input
           resolution. Default: True.
       spynet_path (str): Path to the pretrained weights of SPyNet. Default: None.
       cpu_cache_length (int, optional): When the length of sequence is larger
           than this value, the intermediate features are sent to CPU. This
           saves GPU memory, but slows down the inference speed. You can
           increase this number if you have a GPU with large memory.
           Default: 100.
   """

   def __init__(self,
                mid_channels=64,
                num_blocks=7,
                max_residue_magnitude=10,
                is_low_res_input=True,
                spynet_path=None,
                cpu_cache_length=100):

       super().__init__()
       self.mid_channels = mid_channels
       self.is_low_res_input = is_low_res_input
       self.cpu_cache_length = cpu_cache_length

       # optical flow
       self.spynet = SpyNet(spynet_path)

       # feature extraction module
       if is_low_res_input:
           self.feat_extract1 = nn.Sequential(ConvResidualBlocks(3, mid_channels, 5),
                                              MultiScaleSmallKernelConvAndAttention(mid_channels))
           self.feat_extract2 = nn.Sequential(ConvResidualBlocksModified(3, mid_channels, 5),
                                              MultiScaleLargeKernelConvAndAttention(mid_channels))

       else:
           self.feat_extract = nn.Sequential(
               nn.Conv2d(3, mid_channels, 3, 2, 1), nn.LeakyReLU(negative_slope=0.1, inplace=True),
               nn.Conv2d(mid_channels, mid_channels, 3, 2, 1), nn.LeakyReLU(negative_slope=0.1, inplace=True),
               ConvResidualBlocks(mid_channels, mid_channels, 5), MultiScaleAttention(mid_channels))

           # new branch for multi-scale features
           self.feat_extract_2 = nn.Sequential(
               nn.Conv2d(3, mid_channels, 5, 1, 2), nn.LeakyReLU(negative_slope=0.1, inplace=True),
               nn.Conv2d(mid_channels, mid_channels, 5, 1, 2), nn.LeakyReLU(negative_slope=0.1, inplace=True),
               ConvResidualBlocks(mid_channels, mid_channels, 5), MultiScaleAttention(mid_channels))

       # propagation branches
       self.deform_align = nn.ModuleDict()
       self.backbone = nn.ModuleDict()
       self.fusion = nn.ModuleDict()
       modules = ['backward_1', 'forward_1']
       for i, module in enumerate(modules):
           if torch.cuda.is_available():
               self.deform_align[module] = SecondOrderDeformableAlignment(
                   2 * mid_channels,
                   mid_channels,
                   3,
                   padding=1,
                   deformable_groups=16,
                   max_residue_magnitude=max_residue_magnitude)
           self.backbone[module] = ConvResidualBlocks((1 + i) * mid_channels, mid_channels, num_blocks)
           self.fusion[module] = TSAFusion(num_feat=mid_channels, num_frame=2, center_frame_idx=0)

       # upsampling module
       self.reconstruction = ConvResidualBlocks(3 * mid_channels, mid_channels, 5)

       self.upconv1 = nn.Conv2d(mid_channels, mid_channels * 4, 3, 1, 1, bias=True)
       self.upconv2 = nn.Conv2d(mid_channels, 64 * 4, 3, 1, 1, bias=True)

       self.pixel_shuffle = nn.PixelShuffle(2)

       self.conv_hr = nn.Conv2d(64, 64, 3, 1, 1)
       self.conv_last = nn.Conv2d(64, 3, 3, 1, 1)
       self.img_upsample = nn.Upsample(scale_factor=4, mode='bilinear', align_corners=False)

       # activation function
       self.lrelu = nn.LeakyReLU(negative_slope=0.1, inplace=True)

       # check if the sequence is augmented by flipping
       self.is_mirror_extended = False

       if len(self.deform_align) > 0:
           self.is_with_alignment = True
       else:
           self.is_with_alignment = False
           warnings.warn('Deformable alignment module is not added. '
                         'Probably your CUDA is not configured correctly. DCN can only '
                         'be used with CUDA enabled. Alignment is skipped now.')

   def check_if_mirror_extended(self, lqs):
       """Check whether the input is a mirror-extended sequence.
       If mirror-extended, the i-th (i=0, ..., t-1) frame is equal to the
       (t-1-i)-th frame.
       Args:
           lqs (tensor): Input low quality (LQ) sequence with
               shape (n, t, c, h, w).
       """

       if lqs.size(1) % 2 == 0:
           lqs_1, lqs_2 = torch.chunk(lqs, 2, dim=1)
           if torch.norm(lqs_1 - lqs_2.flip(1)) == 0:
               self.is_mirror_extended = True

   def compute_flow(self, lqs):
       """Compute optical flow using SPyNet for feature alignment.
       Note that if the input is an mirror-extended sequence, 'flows_forward'
       is not needed, since it is equal to 'flows_backward.flip(1)'.
       Args:
           lqs (tensor): Input low quality (LQ) sequence with
               shape (n, t, c, h, w).
       Return:
           tuple(Tensor): Optical flow. 'flows_forward' corresponds to the
               flows used for forward-time propagation (current to previous).
               'flows_backward' corresponds to the flows used for
               backward-time propagation (current to next).
       """

       n, t, c, h, w = lqs.size()
       lqs_1 = lqs[:, :-1, :, :, :].reshape(-1, c, h, w)
       lqs_2 = lqs[:, 1:, :, :, :].reshape(-1, c, h, w)

       flows_backward = self.spynet(lqs_1, lqs_2).view(n, t - 1, 2, h, w)

       if self.is_mirror_extended:
           flows_forward = flows_backward.flip(1)
       else:
           flows_forward = self.spynet(lqs_2, lqs_1).view(n, t - 1, 2, h, w)

       if self.cpu_cache:
           flows_backward = flows_backward.cpu()
           flows_forward = flows_forward.cpu()

       return flows_forward, flows_backward

   def propagate(self, feats, flows, module_name):
       """Propagate the latent features throughout the sequence.
       Args:
           feats dict(list[tensor]): Features from previous branches. Each
               component is a list of tensors with shape (n, c, h, w).
           flows (tensor): Optical flows with shape (n, t - 1, 2, h, w).
           module_name (str): The name of the propgation branches. Can either
               be 'backward_1', 'forward_1', 'backward_2', 'forward_2'.
       Return:
           dict(list[tensor]): A dictionary containing all the propagated
               features. Each key in the dictionary corresponds to a
               propagation branch, which is represented by a list of tensors.
       """

       n, t, _, h, w = flows.size()

       frame_idx = range(0, t + 1)
       flow_idx = range(-1, t)
       mapping_idx = list(range(0, len(feats['spatial'])))
       mapping_idx += mapping_idx[::-1]

       if 'backward' in module_name:
           frame_idx = frame_idx[::-1]
           flow_idx = frame_idx

       feat_prop = flows.new_zeros(n, self.mid_channels, h, w)
       for i, idx in enumerate(frame_idx):
           feat_current = feats['spatial'][mapping_idx[idx]]
           if self.cpu_cache:
               feat_current = feat_current.cuda()
               feat_prop = feat_prop.cuda()
           # second-order deformable alignment
           if i > 0 and self.is_with_alignment:
               flow_n1 = flows[:, flow_idx[i], :, :, :]
               if self.cpu_cache:
                   flow_n1 = flow_n1.cuda()

               cond_n1 = flow_warp(feat_prop, flow_n1.permute(0, 2, 3, 1))

               # initialize second-order features
               feat_n2 = torch.zeros_like(feat_prop)
               flow_n2 = torch.zeros_like(flow_n1)
               cond_n2 = torch.zeros_like(cond_n1)

               if i > 1:  # second-order features
                   feat_n2 = feats[module_name][-2]
                   if self.cpu_cache:
                       feat_n2 = feat_n2.cuda()

                   flow_n2 = flows[:, flow_idx[i - 1], :, :, :]
                   if self.cpu_cache:
                       flow_n2 = flow_n2.cuda()

                   flow_n2 = flow_n1 + flow_warp(flow_n2, flow_n1.permute(0, 2, 3, 1))
                   cond_n2 = flow_warp(feat_n2, flow_n2.permute(0, 2, 3, 1))

               # flow-guided deformable convolution
               cond = torch.cat([cond_n1, feat_current, cond_n2], dim=1)
               feat_prop = torch.cat([feat_prop, feat_n2], dim=1)
               feat_prop = self.deform_align[module_name](feat_prop, cond, flow_n1, flow_n2)

           # concatenate and residual blocks
           last_feat = [feats[k][idx] for k in feats if k not in ['spatial', module_name]]

           feat_current = torch.cat([feat_current] + last_feat, dim=1)
           feat_current = self.backbone[module_name](feat_current)
           feat = [feat_current] + [feat_prop]

           if self.cpu_cache:
               feat = [f.cuda() for f in feat]

           feat = torch.stack(feat, dim=1)

           feat_prop = feat_prop + self.fusion[module_name](feat)

           feats[module_name].append(feat_prop)

           if self.cpu_cache:
               feats[module_name][-1] = feats[module_name][-1].cpu()
               torch.cuda.empty_cache()

       if 'backward' in module_name:
           feats[module_name] = feats[module_name][::-1]

       return feats

   def upsample(self, lqs, feats):
       """Compute the output image given the features.
       Args:
           lqs (tensor): Input low quality (LQ) sequence with
               shape (n, t, c, h, w).
           feats (dict): The features from the propgation branches.
       Returns:
           Tensor: Output HR sequence with shape (n, t, c, 4h, 4w).
       """

       outputs = []
       num_outputs = len(feats['spatial'])

       mapping_idx = list(range(0, num_outputs))
       mapping_idx += mapping_idx[::-1]

       for i in range(0, lqs.size(1)):
           hr = [feats[k].pop(0) for k in feats if k != 'spatial']
           hr.insert(0, feats['spatial'][mapping_idx[i]])
           hr = torch.cat(hr, dim=1)
           if self.cpu_cache:
               hr = hr.cuda()

           hr = self.reconstruction(hr)
           hr = self.lrelu(self.pixel_shuffle(self.upconv1(hr)))
           hr = self.lrelu(self.pixel_shuffle(self.upconv2(hr)))
           hr = self.lrelu(self.conv_hr(hr))

           hr = self.conv_last(hr)
           if self.is_low_res_input:
               hr += self.img_upsample(lqs[:, i, :, :, :])
           else:
               hr += lqs[:, i, :, :, :]

           if self.cpu_cache:
               hr = hr.cpu()
               torch.cuda.empty_cache()

           outputs.append(hr)

       return torch.stack(outputs, dim=1)

   def forward(self, lqs):
       """Forward function for BasicVSR++.
       Args:
           lqs (tensor): Input low quality (LQ) sequence with
               shape (n, t, c, h, w).
       Returns:
           Tensor: Output HR sequence with shape (n, t, c, 4h, 4w).
       """

       save_dir = 'features'

       n, t, c, h, w = lqs.size()

       # whether to cache the features in CPU
       self.cpu_cache = True if t > self.cpu_cache_length else False

       if self.is_low_res_input:
           lqs_downsample = lqs.clone()
       else:
           lqs_downsample = F.interpolate(
               lqs.view(-1, c, h, w), scale_factor=0.25, mode='bicubic').view(n, t, c, h // 4, w // 4)

       # check whether the input is an extended sequence
       self.check_if_mirror_extended(lqs)

       feats = {}
       # compute spatial features
       if self.cpu_cache:
           feats['spatial'] = []
           for i in range(0, t):
               feats_1 = self.feat_extract1(lqs.view(-1, c, h, w))
               feats_2 = self.feat_extract2(lqs.view(-1, c, h, w))
               feats_2 = F.interpolate(feats_2, size=feats_1.shape[2:], mode='bilinear', align_corners=False)
               feat = feats_1 + feats_2
               feats['spatial'].append(feat)
               torch.cuda.empty_cache()
       else:
           feats_1 = self.feat_extract1(lqs.view(-1, c, h, w))
           feats_2 = self.feat_extract2(lqs.view(-1, c, h, w))
           feats_2 = F.interpolate(feats_2, size=feats_1.shape[2:], mode='bilinear', align_corners=False)
           feats_ = feats_1 + feats_2
           h, w = feats_.shape[2:]
           feats_ = feats_.view(n, t, -1, h, w)
           feats['spatial'] = [feats_[:, i, :, :, :] for i in range(0, t)]

       # compute optical flow using the low-res inputs
       assert lqs_downsample.size(3) >= 64 and lqs_downsample.size(4) >= 64, (
           'The height and width of low-res inputs must be at least 64, '
           f'but got {h} and {w}.')
       flows_forward, flows_backward = self.compute_flow(lqs_downsample)

       # feature propgation
       for iter_ in [1]:
           for direction in ['backward', 'forward']:
               module = f'{direction}_{iter_}'

               feats[module] = []

               if direction == 'backward':
                   flows = flows_backward
               elif flows_forward is not None:
                   flows = flows_forward
               else:
                   flows = flows_backward.flip(1)

               feats = self.propagate(feats, flows, module)
               if self.cpu_cache:
                   del flows
                   torch.cuda.empty_cache()

       return self.upsample(lqs, feats)


class SecondOrderDeformableAlignment(ModulatedDeformConvPack):
   """Second-order deformable alignment module.
   Args:
       in_channels (int): Same as nn.Conv2d.
       out_channels (int): Same as nn.Conv2d.
       kernel_size (int or tuple[int]): Same as nn.Conv2d.
       stride (int or tuple[int]): Same as nn.Conv2d.
       padding (int or tuple[int]): Same as nn.Conv2d.
       dilation (int or tuple[int]): Same as nn.Conv2d.
       groups (int): Same as nn.Conv2d.
       bias (bool or str): If specified as `auto`, it will be decided by the
           norm_cfg. Bias will be set as True if norm_cfg is None, otherwise
           False.
       max_residue_magnitude (int): The maximum magnitude of the offset
           residue (Eq. 6 in paper). Default: 10.
   """

   def __init__(self, *args, **kwargs):
       self.max_residue_magnitude = kwargs.pop('max_residue_magnitude', 10)

       super(SecondOrderDeformableAlignment, self).__init__(*args, **kwargs)

       self.conv_offset = nn.Sequential(
           nn.Conv2d(3 * self.out_channels + 4, self.out_channels, 3, 1, 1),
           nn.LeakyReLU(negative_slope=0.1, inplace=True),
           nn.Conv2d(self.out_channels, self.out_channels, 3, 1, 1),
           nn.LeakyReLU(negative_slope=0.1, inplace=True),
           nn.Conv2d(self.out_channels, self.out_channels, 3, 1, 1),
           nn.LeakyReLU(negative_slope=0.1, inplace=True),
           nn.Conv2d(self.out_channels, 27 * self.deformable_groups, 3, 1, 1),
       )

       self.init_offset()

   def init_offset(self):

       def _constant_init(module, val, bias=0):
           if hasattr(module, 'weight') and module.weight is not None:
               nn.init.constant_(module.weight, val)
           if hasattr(module, 'bias') and module.bias is not None:
               nn.init.constant_(module.bias, bias)

       _constant_init(self.conv_offset[-1], val=0, bias=0)

   def forward(self, x, extra_feat, flow_1, flow_2):
       extra_feat = torch.cat([extra_feat, flow_1, flow_2], dim=1)
       out = self.conv_offset(extra_feat)
       o1, o2, mask = torch.chunk(out, 3, dim=1)

       # offset
       offset = self.max_residue_magnitude * torch.tanh(torch.cat((o1, o2), dim=1))
       offset_1, offset_2 = torch.chunk(offset, 2, dim=1)
       offset_1 = offset_1 + flow_1.flip(1).repeat(1, offset_1.size(1) // 2, 1, 1)
       offset_2 = offset_2 + flow_2.flip(1).repeat(1, offset_2.size(1) // 2, 1, 1)
       offset = torch.cat([offset_1, offset_2], dim=1)

       # mask
       mask = torch.sigmoid(mask)

       return torchvision.ops.deform_conv2d(x, offset, self.weight, self.bias, self.stride, self.padding,
                                            self.dilation, mask)