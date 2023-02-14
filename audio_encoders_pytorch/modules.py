from math import floor
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import pack, rearrange, reduce, unpack
from einops_exts import rearrange_many
from torch import Tensor
from torchaudio import transforms
from functools import partial

from .utils import closest_power_2, default, exists, groupby, prefix_dict, prod, to_list

"""
Convolutional Modules
"""


def Conv1d(*args, **kwargs) -> nn.Module:
    return nn.Conv1d(*args, **kwargs)


def ConvTranspose1d(*args, **kwargs) -> nn.Module:
    return nn.ConvTranspose1d(*args, **kwargs)


def Downsample1d(
    in_channels: int, out_channels: int, factor: int, kernel_multiplier: int = 2
) -> nn.Module:
    assert kernel_multiplier % 2 == 0, "Kernel multiplier must be even"

    return Conv1d(
        in_channels=in_channels,
        out_channels=out_channels,
        kernel_size=factor * kernel_multiplier + 1,
        stride=factor,
        padding=factor * (kernel_multiplier // 2),
    )


def Upsample1d(in_channels: int, out_channels: int, factor: int) -> nn.Module:
    if factor == 1:
        return Conv1d(
            in_channels=in_channels, out_channels=out_channels, kernel_size=3, padding=1
        )
    return ConvTranspose1d(
        in_channels=in_channels,
        out_channels=out_channels,
        kernel_size=factor * 2,
        stride=factor,
        padding=factor // 2 + factor % 2,
        output_padding=factor % 2,
    )


class ConvBlock1d(nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        *,
        kernel_size: int = 3,
        stride: int = 1,
        padding: int = 1,
        dilation: int = 1,
        num_groups: int = 8,
        use_norm: bool = True,
    ) -> None:
        super().__init__()

        self.groupnorm = (
            nn.GroupNorm(num_groups=num_groups, num_channels=in_channels)
            if use_norm
            else nn.Identity()
        )
        self.activation = nn.SiLU()
        self.project = Conv1d(
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=padding,
            dilation=dilation,
        )

    def forward(self, x: Tensor) -> Tensor:
        x = self.groupnorm(x)
        x = self.activation(x)
        return self.project(x)


class ResnetBlock1d(nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        *,
        kernel_size: int = 3,
        stride: int = 1,
        padding: int = 1,
        dilation: int = 1,
        use_norm: bool = True,
        num_groups: int = 8,
    ) -> None:
        super().__init__()

        self.block1 = ConvBlock1d(
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=padding,
            dilation=dilation,
            use_norm=use_norm,
            num_groups=num_groups,
        )

        self.block2 = ConvBlock1d(
            in_channels=out_channels,
            out_channels=out_channels,
            use_norm=use_norm,
            num_groups=num_groups,
        )

        self.to_out = (
            Conv1d(in_channels=in_channels, out_channels=out_channels, kernel_size=1)
            if in_channels != out_channels
            else nn.Identity()
        )

    def forward(self, x: Tensor) -> Tensor:
        h = self.block1(x)
        h = self.block2(h)
        return h + self.to_out(x)


class Patcher(nn.Module):
    def __init__(self, in_channels: int, out_channels: int, patch_size: int):
        super().__init__()
        assert_message = f"out_channels must be divisible by patch_size ({patch_size})"
        assert out_channels % patch_size == 0, assert_message
        self.patch_size = patch_size

        self.block = ResnetBlock1d(
            in_channels=in_channels,
            out_channels=out_channels // patch_size,
            num_groups=min(patch_size, in_channels),
        )

    def forward(self, x: Tensor) -> Tensor:
        x = self.block(x)
        x = rearrange(x, "b c (l p) -> b (c p) l", p=self.patch_size)
        return x


class Unpatcher(nn.Module):
    def __init__(self, in_channels: int, out_channels: int, patch_size: int):
        super().__init__()
        assert_message = f"in_channels must be divisible by patch_size ({patch_size})"
        assert in_channels % patch_size == 0, assert_message
        self.patch_size = patch_size

        self.block = ResnetBlock1d(
            in_channels=in_channels // patch_size,
            out_channels=out_channels,
            num_groups=min(patch_size, out_channels),
        )

    def forward(self, x: Tensor) -> Tensor:
        x = rearrange(x, " b (c p) l -> b c (l p) ", p=self.patch_size)
        x = self.block(x)
        return x


class DownsampleBlock1d(nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        *,
        factor: int,
        num_groups: int,
        num_layers: int,
    ):
        super().__init__()

        self.downsample = Downsample1d(
            in_channels=in_channels, out_channels=out_channels, factor=factor
        )

        self.blocks = nn.ModuleList(
            [
                ResnetBlock1d(
                    in_channels=out_channels,
                    out_channels=out_channels,
                    num_groups=num_groups,
                )
                for i in range(num_layers)
            ]
        )

    def forward(self, x: Tensor) -> Tensor:
        x = self.downsample(x)
        for block in self.blocks:
            x = block(x)
        return x


class UpsampleBlock1d(nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        *,
        factor: int,
        num_layers: int,
        num_groups: int,
    ):
        super().__init__()

        self.blocks = nn.ModuleList(
            [
                ResnetBlock1d(
                    in_channels=in_channels,
                    out_channels=in_channels,
                    num_groups=num_groups,
                )
                for _ in range(num_layers)
            ]
        )

        self.upsample = Upsample1d(
            in_channels=in_channels, out_channels=out_channels, factor=factor
        )

    def forward(self, x: Tensor) -> Tensor:
        for block in self.blocks:
            x = block(x)
        x = self.upsample(x)
        return x


"""
Encoders / Decoders
"""


class Bottleneck(nn.Module):
    def forward(
        self, x: Tensor, with_info: bool = False
    ) -> Union[Tensor, Tuple[Tensor, Any]]:
        raise NotImplementedError()


class Encoder1d(nn.Module):
    def __init__(
        self,
        in_channels: int,
        channels: int,
        multipliers: Sequence[int],
        factors: Sequence[int],
        num_blocks: Sequence[int],
        patch_size: int = 1,
        resnet_groups: int = 8,
        out_channels: Optional[int] = None,
        bottleneck: Union[Bottleneck, List[Bottleneck]] = [],
    ):
        super().__init__()
        self.bottlenecks = nn.ModuleList(to_list(bottleneck))
        self.num_layers = len(multipliers) - 1
        self.downsample_factor = patch_size * prod(factors)
        self.out_channels = (
            out_channels if exists(out_channels) else channels * multipliers[-1]
        )
        assert len(factors) == self.num_layers and len(num_blocks) == self.num_layers

        self.to_in = Patcher(
            in_channels=in_channels,
            out_channels=channels * multipliers[0],
            patch_size=patch_size,
        )

        self.downsamples = nn.ModuleList(
            [
                DownsampleBlock1d(
                    in_channels=channels * multipliers[i],
                    out_channels=channels * multipliers[i + 1],
                    factor=factors[i],
                    num_groups=resnet_groups,
                    num_layers=num_blocks[i],
                )
                for i in range(self.num_layers)
            ]
        )

        self.to_out = (
            nn.Conv1d(
                in_channels=channels * multipliers[-1],
                out_channels=out_channels,
                kernel_size=1,
            )
            if exists(out_channels)
            else nn.Identity()
        )

    def forward(
        self, x: Tensor, with_info: bool = False
    ) -> Union[Tensor, Tuple[Tensor, Any]]:
        xs = [x]
        x = self.to_in(x)
        xs += [x]

        for downsample in self.downsamples:
            x = downsample(x)
            xs += [x]

        x = self.to_out(x)
        xs += [x]
        info = dict(xs=xs)

        for bottleneck in self.bottlenecks:
            x, info_bottleneck = bottleneck(x, with_info=True)
            info = {**info, **prefix_dict("bottleneck_", info_bottleneck)}

        return (x, info) if with_info else x


class Decoder1d(nn.Module):
    def __init__(
        self,
        out_channels: int,
        channels: int,
        multipliers: Sequence[int],
        factors: Sequence[int],
        num_blocks: Sequence[int],
        patch_size: int = 1,
        resnet_groups: int = 8,
        in_channels: Optional[int] = None,
    ):
        super().__init__()
        num_layers = len(multipliers) - 1

        assert len(factors) == num_layers and len(num_blocks) == num_layers

        self.to_in = (
            Conv1d(
                in_channels=in_channels,
                out_channels=channels * multipliers[0],
                kernel_size=1,
            )
            if exists(in_channels)
            else nn.Identity()
        )

        self.upsamples = nn.ModuleList(
            [
                UpsampleBlock1d(
                    in_channels=channels * multipliers[i],
                    out_channels=channels * multipliers[i + 1],
                    factor=factors[i],
                    num_groups=resnet_groups,
                    num_layers=num_blocks[i],
                )
                for i in range(num_layers)
            ]
        )

        self.to_out = Unpatcher(
            in_channels=channels * multipliers[-1],
            out_channels=out_channels,
            patch_size=patch_size,
        )

    def forward(
        self, x: Tensor, with_info: bool = False
    ) -> Union[Tensor, Tuple[Tensor, Any]]:
        xs = [x]
        x = self.to_in(x)
        xs += [x]

        for upsample in self.upsamples:
            x = upsample(x)
            xs += [x]

        x = self.to_out(x)
        xs += [x]

        info = dict(xs=xs)
        return (x, info) if with_info else x


class AutoEncoder1d(nn.Module):
    def __init__(
        self,
        in_channels: int,
        channels: int,
        multipliers: Sequence[int],
        factors: Sequence[int],
        num_blocks: Sequence[int],
        patch_size: int = 1,
        resnet_groups: int = 8,
        out_channels: Optional[int] = None,
        bottleneck: Union[Bottleneck, List[Bottleneck]] = [],
        bottleneck_channels: Optional[int] = None,
    ):
        super().__init__()
        out_channels = default(out_channels, in_channels)

        self.encoder = Encoder1d(
            in_channels=in_channels,
            out_channels=bottleneck_channels,
            channels=channels,
            multipliers=multipliers,
            factors=factors,
            num_blocks=num_blocks,
            patch_size=patch_size,
            resnet_groups=resnet_groups,
            bottleneck=bottleneck,
        )

        self.decoder = Decoder1d(
            in_channels=bottleneck_channels,
            out_channels=out_channels,
            channels=channels,
            multipliers=multipliers[::-1],
            factors=factors[::-1],
            num_blocks=num_blocks[::-1],
            patch_size=patch_size,
            resnet_groups=resnet_groups,
        )

    def forward(
        self, x: Tensor, with_info: bool = False
    ) -> Union[Tensor, Tuple[Tensor, Any]]:
        z, info_encoder = self.encode(x, with_info=True)
        y, info_decoder = self.decode(z, with_info=True)
        info = {
            **dict(latent=z),
            **prefix_dict("encoder_", info_encoder),
            **prefix_dict("decoder_", info_decoder),
        }
        return (y, info) if with_info else y

    def encode(
        self, x: Tensor, with_info: bool = False
    ) -> Union[Tensor, Tuple[Tensor, Any]]:
        return self.encoder(x, with_info=with_info)

    def decode(self, x: Tensor, with_info: bool = False) -> Tensor:
        return self.decoder(x, with_info=with_info)


class STFT(nn.Module):
    """Helper for torch stft and istft"""

    def __init__(
        self,
        num_fft: int = 1023,
        hop_length: int = 256,
        window_length: Optional[int] = None,
        length: Optional[int] = None,
        use_complex: bool = False,
    ):
        super().__init__()
        self.num_fft = num_fft
        self.hop_length = default(hop_length, floor(num_fft // 4))
        self.window_length = default(window_length, num_fft)
        self.length = length
        self.register_buffer("window", torch.hann_window(self.window_length))
        self.use_complex = use_complex

    def encode(self, wave: Tensor) -> Tuple[Tensor, Tensor]:
        b = wave.shape[0]
        wave = rearrange(wave, "b c t -> (b c) t")

        stft = torch.stft(
            wave,
            n_fft=self.num_fft,
            hop_length=self.hop_length,
            win_length=self.window_length,
            window=self.window,  # type: ignore
            return_complex=True,
            normalized=True,
        )

        if self.use_complex:
            # Returns real and imaginary
            stft_a, stft_b = stft.real, stft.imag
        else:
            # Returns magnitude and phase matrices
            magnitude, phase = torch.abs(stft), torch.angle(stft)
            stft_a, stft_b = magnitude, phase

        return rearrange_many((stft_a, stft_b), "(b c) f l -> b c f l", b=b)

    def decode(self, stft_a: Tensor, stft_b: Tensor) -> Tensor:
        b, l = stft_a.shape[0], stft_a.shape[-1]  # noqa
        length = closest_power_2(l * self.hop_length)

        stft_a, stft_b = rearrange_many((stft_a, stft_b), "b c f l -> (b c) f l")

        if self.use_complex:
            real, imag = stft_a, stft_b
        else:
            magnitude, phase = stft_a, stft_b
            real, imag = magnitude * torch.cos(phase), magnitude * torch.sin(phase)

        stft = torch.stack([real, imag], dim=-1)

        wave = torch.istft(
            stft,
            n_fft=self.num_fft,
            hop_length=self.hop_length,
            win_length=self.window_length,
            window=self.window,  # type: ignore
            length=default(self.length, length),
            normalized=True,
        )

        return rearrange(wave, "(b c) t -> b c t", b=b)

    def encode1d(
        self, wave: Tensor, stacked: bool = True
    ) -> Union[Tensor, Tuple[Tensor, Tensor]]:
        stft_a, stft_b = self.encode(wave)
        stft_a, stft_b = rearrange_many((stft_a, stft_b), "b c f l -> b (c f) l")
        return torch.cat((stft_a, stft_b), dim=1) if stacked else (stft_a, stft_b)

    def decode1d(self, stft_pair: Tensor) -> Tensor:
        f = self.num_fft // 2 + 1
        stft_a, stft_b = stft_pair.chunk(chunks=2, dim=1)
        stft_a, stft_b = rearrange_many((stft_a, stft_b), "b (c f) l -> b c f l", f=f)
        return self.decode(stft_a, stft_b)


class ME1d(Encoder1d):
    """Magnitude Encoder"""

    def __init__(
        self, in_channels: int, stft_num_fft: int, use_log: bool = False, **kwargs
    ):
        self.use_log = use_log
        self.frequency_channels = stft_num_fft // 2 + 1
        stft_kwargs, kwargs = groupby("stft_", kwargs)
        super().__init__(in_channels=in_channels * self.frequency_channels, **kwargs)
        self.stft = STFT(num_fft=stft_num_fft, **stft_kwargs)
        self.downsample_factor *= self.stft.hop_length

    def forward(self, x: Tensor, **kwargs) -> Union[Tensor, Tuple[Tensor, Any]]:  # type: ignore # noqa
        magnitude, _ = self.stft.encode(x)
        magnitude = rearrange(magnitude, "b c f l -> b (c f) l")
        magnitude = torch.log(magnitude) if self.use_log else magnitude
        return super().forward(magnitude, **kwargs)


class MAE1d(AutoEncoder1d):
    """Magnitude Auto Encoder"""

    def __init__(self, in_channels: int, stft_num_fft: int = 1023, **kwargs):
        self.frequency_channels = stft_num_fft // 2 + 1
        stft_kwargs, kwargs = groupby("stft_", kwargs)
        super().__init__(in_channels=in_channels * self.frequency_channels, **kwargs)
        self.stft = STFT(num_fft=stft_num_fft, **stft_kwargs)

    def encode(self, magnitude: Tensor, **kwargs):  # type: ignore
        log_magnitude = torch.log(magnitude)
        log_magnitude_flat = rearrange(log_magnitude, "b c f l -> b (c f) l")
        return super().encode(log_magnitude_flat, **kwargs)

    def decode(  # type: ignore
        self, latent: Tensor, with_info: bool = False
    ) -> Union[Tensor, Tuple[Tensor, Dict]]:
        f = self.frequency_channels
        log_magnitude_flat, info = super().decode(latent, with_info=True)
        log_magnitude = rearrange(log_magnitude_flat, "b (c f) l -> b c f l", f=f)
        log_magnitude = torch.clamp(log_magnitude, -30.0, 20.0)
        magnitude = torch.exp(log_magnitude)
        info = dict(log_magnitude=log_magnitude, **info)
        return (magnitude, info) if with_info else magnitude

    def loss(
        self, wave: Tensor, with_info: bool = False
    ) -> Union[Tensor, Tuple[Tensor, Dict]]:
        magnitude, _ = self.stft.encode(wave)
        magnitude_pred, info = self(magnitude, with_info=True)
        loss = F.l1_loss(torch.log(magnitude), torch.log(magnitude_pred))
        return (loss, info) if with_info else loss


class MelSpectrogram(nn.Module):
    def __init__(
        self,
        device: torch.device,
        n_fft: int = 1024,
        hop_length: int = 256,
        win_length: int = 1024,
        sample_rate: int = 48000,
        n_mel_channels: int = 80,
        center: bool = False,
        normalize: bool = False,
        normalize_log: bool = False,
    ):
        super().__init__()
        self.padding = (n_fft - hop_length) // 2
        self.normalize = normalize
        self.normalize_log = normalize_log
        self.hop_length = hop_length

        self.to_spectrogram = transforms.Spectrogram(
            n_fft=n_fft,
            hop_length=hop_length,
            win_length=win_length,
            center=center,
            power=None,
            window_fn=partial(torch.hann_window, device=device)
        )

        self.to_mel_scale = transforms.MelScale(
            n_mels=n_mel_channels, n_stft=n_fft // 2 + 1, sample_rate=sample_rate
        )

    def forward(self, waveform: Tensor) -> Tensor:
        # Pack non-time dimension
        waveform, ps = pack([waveform], "* t")
        # Pad waveform
        waveform = F.pad(waveform, [self.padding] * 2, mode="reflect")
        # Compute STFT
        spectrogram = self.to_spectrogram(waveform)
        # Compute magnitude
        spectrogram = torch.abs(spectrogram)
        # Convert to mel scale
        mel_spectrogram = self.to_mel_scale(spectrogram)
        # Normalize
        if self.normalize:
            mel_spectrogram = mel_spectrogram / torch.max(mel_spectrogram)
            mel_spectrogram = 2 * torch.pow(mel_spectrogram, 0.25) - 1
        if self.normalize_log:
            mel_spectrogram = torch.log(torch.clamp(mel_spectrogram, min=1e-5))
        # Unpack non-spectrogram dimension
        return unpack(mel_spectrogram, ps, "* f l")[0]


class MelE1d(Encoder1d):
    """Magnitude Encoder"""

    def __init__(self, in_channels: int, mel_channels: int, device: torch.device, **kwargs):
        mel_kwargs, kwargs = groupby("mel_", kwargs)
        super().__init__(in_channels=in_channels * mel_channels, device=device, **kwargs)
        self.mel = MelSpectrogram(n_mel_channels=mel_channels, **mel_kwargs)
        self.downsample_factor *= self.mel.hop_length

    def forward(self, x: Tensor, **kwargs) -> Union[Tensor, Tuple[Tensor, Any]]:  # type: ignore # noqa
        mel = rearrange(self.mel(x), "b c f l -> b (c f) l")
        return super().forward(mel, **kwargs)


"""
Bottlenecks
"""


def gaussian_sample(mean: Tensor, logvar: Tensor) -> Tensor:
    std = torch.exp(0.5 * logvar)
    eps = torch.randn_like(std)
    sample = mean + std * eps
    return sample


def kl_loss(mean: Tensor, logvar: Tensor) -> Tensor:
    losses = mean**2 + logvar.exp() - logvar - 1
    loss = reduce(losses, "b ... -> 1", "mean").item()
    return loss


class VariationalBottleneck(Bottleneck):
    def __init__(self, channels: int, loss_weight: float = 1.0):
        super().__init__()
        self.loss_weight = loss_weight
        self.to_mean_and_std = Conv1d(
            in_channels=channels,
            out_channels=channels * 2,
            kernel_size=1,
        )

    def forward(
        self, x: Tensor, with_info: bool = False
    ) -> Union[Tensor, Tuple[Tensor, Any]]:
        mean_and_std = self.to_mean_and_std(x)
        mean, std = mean_and_std.chunk(chunks=2, dim=1)
        mean = torch.tanh(mean)  # mean in range [-1, 1]
        std = torch.tanh(std) + 1.0  # std in range [0, 2]
        out = gaussian_sample(mean, std)
        info = dict(
            variational_kl_loss=kl_loss(mean, std) * self.loss_weight,
            variational_mean=mean,
            variational_std=std,
        )
        return (out, info) if with_info else out


class TanhBottleneck(Bottleneck):
    def forward(
        self, x: Tensor, with_info: bool = False
    ) -> Union[Tensor, Tuple[Tensor, Any]]:
        x = torch.tanh(x)
        info: Dict = dict()
        return (x, info) if with_info else x


class NoiserBottleneck(Bottleneck):
    def __init__(self, sigma: float = 1.0):
        super().__init__()
        self.sigma = sigma

    def forward(
        self, x: Tensor, with_info: bool = False
    ) -> Union[Tensor, Tuple[Tensor, Any]]:
        if self.training:
            x = torch.randn_like(x) * self.sigma + x
        info: Dict = dict()
        return (x, info) if with_info else x


class BitcodesBottleneck(Bottleneck):
    def __init__(self, channels: int, num_bits: int, temperature: float = 1.0):
        super().__init__()
        from bitcodes_pytorch import Bitcodes

        self.bitcodes = Bitcodes(
            features=channels, num_bits=num_bits, temperature=temperature
        )

    def forward(
        self, x: Tensor, with_info: bool = False
    ) -> Union[Tensor, Tuple[Tensor, Any]]:
        x = rearrange(x, "b c t -> b t c")
        x, bits = self.bitcodes(x)
        x = rearrange(x, "b t c -> b c t")
        info: Dict = dict(bits=bits)
        return (x, info) if with_info else x


"""
Discriminators
"""


class Discriminator1d(nn.Module):
    def __init__(self, use_loss: Optional[Sequence[bool]] = None, **kwargs):
        super().__init__()
        self.discriminator = Encoder1d(**kwargs)
        num_layers = self.discriminator.num_layers
        # By default we activate discrimination loss extraction on all layers
        self.use_loss = default(use_loss, [True] * num_layers)
        # Check correct length
        msg = f"use_loss length must match the number of layers ({num_layers})"
        assert len(self.use_loss) == num_layers, msg

    def forward(
        self, true: Tensor, fake: Tensor, with_info: bool = False
    ) -> Union[Tuple[Tensor, Tensor], Tuple[Tensor, Tensor, Dict]]:
        # Get discriminator outputs for true/fake scores
        _, info_true = self.discriminator(true, with_info=True)
        _, info_fake = self.discriminator(fake, with_info=True)

        # Get all intermediate layer features (ignore input)
        xs_true = info_true["xs"][1:]
        xs_fake = info_fake["xs"][1:]

        loss_gs, loss_ds, scores_true, scores_fake = [], [], [], []

        for use_loss, x_true, x_fake in zip(self.use_loss, xs_true, xs_fake):
            if use_loss:
                # Half the channels are used for scores, the other for features
                score_true, feat_true = x_true.chunk(chunks=2, dim=1)
                score_fake, feat_fake = x_fake.chunk(chunks=2, dim=1)
                # Generator must match features with true sample and fool discriminator
                loss_gs += [F.l1_loss(feat_true, feat_fake) - score_fake.mean()]
                # Discriminator must give high score to true samples, low to fake
                loss_ds += [((1 - score_true).relu() + (1 + score_fake).relu()).mean()]
                # Save scores
                scores_true += [score_true.mean()]
                scores_fake += [score_fake.mean()]

        # Average all generator/discriminator losses over all layers
        loss_g = torch.stack(loss_gs).mean()
        loss_d = torch.stack(loss_ds).mean()

        info = dict(scores_true=scores_true, scores_fake=scores_fake)

        return (loss_g, loss_d, info) if with_info else (loss_g, loss_d)
