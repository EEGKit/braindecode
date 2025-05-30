# Authors: Robin Tibor Schirrmeister <robintibor@gmail.com>
#          Tonio Ball
#
# License: BSD-3

import numpy as np
import torch
from einops.layers.torch import Rearrange
from torch import nn
from torch.nn import init

from braindecode.models.base import EEGModuleMixin
from braindecode.modules import (
    AvgPool2dWithConv,
    Ensure4d,
    SqueezeFinalOutput,
)


class EEGResNet(EEGModuleMixin, nn.Sequential):
    """EEGResNet from Schirrmeister et al. 2017 [Schirrmeister2017]_.

    .. figure:: https://onlinelibrary.wiley.com/cms/asset/bed1b768-809f-4bc6-b942-b36970d81271/hbm23730-fig-0003-m.jpg
        :align: center
        :alt: EEGResNet Architecture

    Model described in [Schirrmeister2017]_.

    Parameters
    ----------
    in_chans :
        Alias for ``n_chans``.
    n_classes :
        Alias for ``n_outputs``.
    input_window_samples :
       Alias for ``n_times``.
    activation: nn.Module, default=nn.ELU
        Activation function class to apply. Should be a PyTorch activation
        module class like ``nn.ReLU`` or ``nn.ELU``. Default is ``nn.ELU``.

    References
    ----------
    .. [Schirrmeister2017] Schirrmeister, R. T., Springenberg, J. T., Fiederer,
       L. D. J., Glasstetter, M., Eggensperger, K., Tangermann, M., Hutter, F.
       & Ball, T. (2017). Deep learning with convolutional neural networks for ,
       EEG decoding and visualization. Human Brain Mapping, Aug. 2017.
       Online: http://dx.doi.org/10.1002/hbm.23730
    """

    def __init__(
        self,
        n_chans=None,
        n_outputs=None,
        n_times=None,
        final_pool_length="auto",
        n_first_filters=20,
        n_layers_per_block=2,
        first_filter_length=3,
        activation=nn.ELU,
        split_first_layer=True,
        batch_norm_alpha=0.1,
        batch_norm_epsilon=1e-4,
        conv_weight_init_fn=lambda w: init.kaiming_normal_(w, a=0),
        chs_info=None,
        input_window_seconds=None,
        sfreq=250,
    ):
        super().__init__(
            n_outputs=n_outputs,
            n_chans=n_chans,
            chs_info=chs_info,
            n_times=n_times,
            input_window_seconds=input_window_seconds,
            sfreq=sfreq,
        )
        del n_outputs, n_chans, chs_info, n_times, input_window_seconds, sfreq

        if final_pool_length == "auto":
            assert self.n_times is not None
        assert first_filter_length % 2 == 1
        self.final_pool_length = final_pool_length
        self.n_first_filters = n_first_filters
        self.n_layers_per_block = n_layers_per_block
        self.first_filter_length = first_filter_length
        self.nonlinearity = activation
        self.split_first_layer = split_first_layer
        self.batch_norm_alpha = batch_norm_alpha
        self.batch_norm_epsilon = batch_norm_epsilon
        self.conv_weight_init_fn = conv_weight_init_fn

        self.mapping = {
            "conv_classifier.weight": "final_layer.conv_classifier.weight",
            "conv_classifier.bias": "final_layer.conv_classifier.bias",
        }

        self.add_module("ensuredims", Ensure4d())
        if self.split_first_layer:
            self.add_module("dimshuffle", Rearrange("batch C T 1 -> batch 1 T C"))
            self.add_module(
                "conv_time",
                nn.Conv2d(
                    1,
                    self.n_first_filters,
                    (self.first_filter_length, 1),
                    stride=1,
                    padding=(self.first_filter_length // 2, 0),
                ),
            )
            self.add_module(
                "conv_spat",
                nn.Conv2d(
                    self.n_first_filters,
                    self.n_first_filters,
                    (1, self.n_chans),
                    stride=(1, 1),
                    bias=False,
                ),
            )
        else:
            self.add_module(
                "conv_time",
                nn.Conv2d(
                    self.n_chans,
                    self.n_first_filters,
                    (self.first_filter_length, 1),
                    stride=(1, 1),
                    padding=(self.first_filter_length // 2, 0),
                    bias=False,
                ),
            )
        n_filters_conv = self.n_first_filters
        self.add_module(
            "bnorm",
            nn.BatchNorm2d(
                n_filters_conv, momentum=self.batch_norm_alpha, affine=True, eps=1e-5
            ),
        )
        self.add_module("conv_nonlin", self.nonlinearity())
        cur_dilation = np.array([1, 1])
        n_cur_filters = n_filters_conv
        i_block = 1
        for i_layer in range(self.n_layers_per_block):
            self.add_module(
                "res_{:d}_{:d}".format(i_block, i_layer),
                _ResidualBlock(n_cur_filters, n_cur_filters, dilation=cur_dilation),
            )
        i_block += 1
        cur_dilation[0] *= 2
        n_out_filters = int(2 * n_cur_filters)
        self.add_module(
            "res_{:d}_{:d}".format(i_block, 0),
            _ResidualBlock(
                n_cur_filters,
                n_out_filters,
                dilation=cur_dilation,
            ),
        )
        n_cur_filters = n_out_filters
        for i_layer in range(1, self.n_layers_per_block):
            self.add_module(
                "res_{:d}_{:d}".format(i_block, i_layer),
                _ResidualBlock(n_cur_filters, n_cur_filters, dilation=cur_dilation),
            )

        i_block += 1
        cur_dilation[0] *= 2
        n_out_filters = int(1.5 * n_cur_filters)
        self.add_module(
            "res_{:d}_{:d}".format(i_block, 0),
            _ResidualBlock(
                n_cur_filters,
                n_out_filters,
                dilation=cur_dilation,
            ),
        )
        n_cur_filters = n_out_filters
        for i_layer in range(1, self.n_layers_per_block):
            self.add_module(
                "res_{:d}_{:d}".format(i_block, i_layer),
                _ResidualBlock(n_cur_filters, n_cur_filters, dilation=cur_dilation),
            )

        i_block += 1
        cur_dilation[0] *= 2
        self.add_module(
            "res_{:d}_{:d}".format(i_block, 0),
            _ResidualBlock(
                n_cur_filters,
                n_cur_filters,
                dilation=cur_dilation,
            ),
        )
        for i_layer in range(1, self.n_layers_per_block):
            self.add_module(
                "res_{:d}_{:d}".format(i_block, i_layer),
                _ResidualBlock(n_cur_filters, n_cur_filters, dilation=cur_dilation),
            )

        i_block += 1
        cur_dilation[0] *= 2
        self.add_module(
            "res_{:d}_{:d}".format(i_block, 0),
            _ResidualBlock(
                n_cur_filters,
                n_cur_filters,
                dilation=cur_dilation,
            ),
        )
        for i_layer in range(1, self.n_layers_per_block):
            self.add_module(
                "res_{:d}_{:d}".format(i_block, i_layer),
                _ResidualBlock(n_cur_filters, n_cur_filters, dilation=cur_dilation),
            )

        i_block += 1
        cur_dilation[0] *= 2
        self.add_module(
            "res_{:d}_{:d}".format(i_block, 0),
            _ResidualBlock(
                n_cur_filters,
                n_cur_filters,
                dilation=cur_dilation,
            ),
        )
        for i_layer in range(1, self.n_layers_per_block):
            self.add_module(
                "res_{:d}_{:d}".format(i_block, i_layer),
                _ResidualBlock(n_cur_filters, n_cur_filters, dilation=cur_dilation),
            )
        i_block += 1
        cur_dilation[0] *= 2
        self.add_module(
            "res_{:d}_{:d}".format(i_block, 0),
            _ResidualBlock(
                n_cur_filters,
                n_cur_filters,
                dilation=cur_dilation,
            ),
        )
        for i_layer in range(1, self.n_layers_per_block):
            self.add_module(
                "res_{:d}_{:d}".format(i_block, i_layer),
                _ResidualBlock(n_cur_filters, n_cur_filters, dilation=cur_dilation),
            )

        self.eval()
        if self.final_pool_length == "auto":
            self.add_module("mean_pool", nn.AdaptiveAvgPool2d((1, 1)))
        else:
            pool_dilation = int(cur_dilation[0]), int(cur_dilation[1])
            self.add_module(
                "mean_pool",
                AvgPool2dWithConv(
                    (self.final_pool_length, 1), (1, 1), dilation=pool_dilation
                ),
            )

        # Incorporating classification module and subsequent ones in one final layer
        module = nn.Sequential()

        module.add_module(
            "conv_classifier",
            nn.Conv2d(
                n_cur_filters,
                self.n_outputs,
                (1, 1),
                bias=True,
            ),
        )

        module.add_module("squeeze", SqueezeFinalOutput())

        self.add_module("final_layer", module)

        # Initialize all weights
        self.apply(lambda module: self._weights_init(module, self.conv_weight_init_fn))

        # Start in train mode
        self.train()

    @staticmethod
    def _weights_init(module, conv_weight_init_fn):
        """
        initialize weights
        """
        classname = module.__class__.__name__
        if "Conv" in classname and classname != "AvgPool2dWithConv":
            conv_weight_init_fn(module.weight)
            if module.bias is not None:
                init.constant_(module.bias, 0)
        elif "BatchNorm" in classname:
            init.constant_(module.weight, 1)
            init.constant_(module.bias, 0)


class _ResidualBlock(nn.Module):
    """
    create a residual learning building block with two stacked 3x3 convlayers as in paper
    """

    def __init__(
        self,
        in_filters,
        out_num_filters,
        dilation,
        filter_time_length=3,
        nonlinearity: nn.Module = nn.ELU,
        batch_norm_alpha=0.1,
        batch_norm_epsilon=1e-4,
    ):
        super(_ResidualBlock, self).__init__()
        time_padding = int((filter_time_length - 1) * dilation[0])
        assert time_padding % 2 == 0
        time_padding = int(time_padding // 2)
        dilation = (int(dilation[0]), int(dilation[1]))
        assert (out_num_filters - in_filters) % 2 == 0, (
            "Need even number of extra channels in order to be able to pad correctly"
        )
        self.n_pad_chans = out_num_filters - in_filters

        self.conv_1 = nn.Conv2d(
            in_filters,
            out_num_filters,
            (filter_time_length, 1),
            stride=(1, 1),
            dilation=dilation,
            padding=(time_padding, 0),
        )
        self.bn1 = nn.BatchNorm2d(
            out_num_filters,
            momentum=batch_norm_alpha,
            affine=True,
            eps=batch_norm_epsilon,
        )
        self.conv_2 = nn.Conv2d(
            out_num_filters,
            out_num_filters,
            (filter_time_length, 1),
            stride=(1, 1),
            dilation=dilation,
            padding=(time_padding, 0),
        )
        self.bn2 = nn.BatchNorm2d(
            out_num_filters,
            momentum=batch_norm_alpha,
            affine=True,
            eps=batch_norm_epsilon,
        )
        # also see https://mail.google.com/mail/u/0/#search/ilya+joos/1576137dd34c3127
        # for resnet options as ilya used them
        self.nonlinearity = nonlinearity()

    def forward(self, x):
        stack_1 = self.nonlinearity(self.bn1(self.conv_1(x)))
        stack_2 = self.bn2(self.conv_2(stack_1))  # next nonlin after sum
        if self.n_pad_chans != 0:
            zeros_for_padding = x.new_zeros(
                (x.shape[0], self.n_pad_chans // 2, x.shape[2], x.shape[3])
            )
            x = torch.cat((zeros_for_padding, x, zeros_for_padding), dim=1)
        out = self.nonlinearity(x + stack_2)
        return out
