# neural-texturize — Copyright (c) 2020, Novelty Factory KG.  See LICENSE for details.

import torch
import torch.nn.functional as F

from .io import load_tensor_from_image
from .app import Application, Result
from .critics import PatchCritic, GramMatrixCritic, HistogramCritic


def create_default_critics(mode):
    if mode == "gram":
        layers = ("1_1", "1_1:2_1", "2_1", "2_1:3_1", "3_1")
    else:
        layers = ("3_1", "2_1", "1_1")

    if mode == "patch":
        return {l: PatchCritic(layer=l) for l in layers}
    elif mode == "gram":
        return {l: GramMatrixCritic(layer=l) for l in layers}
    elif mode == "hist":
        return {l: HistogramCritic(layer=l) for l in layers}


class Command:
    def prepare_critics(self, app, scale):
        raise NotImplementedError

    def prepare_seed_tensor(self, size, previous=None):
        raise NotImplementedError

    def finalize_octave(self, result):
        return result

    def _prepare_critics(self, app, scale, texture, critics):
        texture_cur = F.interpolate(
            texture,
            scale_factor=1.0 / scale,
            mode="area",
            recompute_scale_factor=False,
        ).to(device=app.device, dtype=app.precision)

        layers = [c.get_layers() for c in critics]
        feats = dict(app.encoder.extract(texture_cur, layers))
        for critic in critics:
            critic.from_features(feats)
        app.log.debug("<- texture:", tuple(texture_cur.shape[2:]))


class Remix(Command):
    def __init__(self, source, mode="patch"):
        self.critics = list(create_default_critics(mode).values())
        self.source = load_tensor_from_image(source, device="cpu")

    def prepare_critics(self, app, scale):
        self._prepare_critics(app, scale, self.source, self.critics)
        return [self.critics]

    def prepare_seed_tensor(self, app, size, previous=None):
        if previous is None:
            b, _, h, w = size
            mean = self.source.mean(dim=(2, 3), keepdim=True).to(device=app.device)
            result = torch.empty((b, 1, h, w), device=app.device, dtype=torch.float32)
            return (
                (result.normal_(std=0.1) + mean).clamp(0.0, 1.0).to(dtype=app.precision)
            )

        return F.interpolate(
            previous, size=size[2:], mode="bicubic", align_corners=False,
        ).clamp_(0.0, 1.0)


class Remake(Command):
    def __init__(self, target, source, mode="gram", weights=[1.0]):
        self.critics = list(create_default_critics(mode).values())
        self.source = load_tensor_from_image(source, device="cpu")
        self.target = load_tensor_from_image(target, device="cpu")
        self.weights = torch.tensor(weights, dtype=torch.float32).view(-1, 1, 1, 1)

    def prepare_critics(self, app, scale):
        self._prepare_critics(app, scale, self.source, self.critics)
        return [self.critics]

    def prepare_seed_tensor(self, app, size, previous=None):
        seed = F.interpolate(
            self.target.to(device=app.device),
            size=size[2:],
            mode="bicubic",
            align_corners=False,
        )

        source_mean = self.source.mean(dim=(2, 3), keepdim=True).to(app.device)
        source_std = self.source.std(dim=(2, 3), keepdim=True).to(app.device)
        seed_mean = seed.mean(dim=(2, 3), keepdim=True)
        seed_std = seed.std(dim=(2, 3), keepdim=True)

        result = source_mean + source_std * ((seed - seed_mean) / seed_std)
        return result.clamp(0.0, 1.0).to(dtype=app.precision)

    def finalize_octave(self, result):
        device = result.images.device
        weights = self.weights.to(device)
        images = result.images.expand(len(self.weights), -1, -1, -1)
        target = self.target.to(device).expand(len(self.weights), -1, -1, -1)
        return Result(images * (weights + 0.0) + (1.0 - weights) * target, *result[1:])


class Mashup(Command):
    def __init__(self, sources, mode="gram"):
        self.critics = list(create_default_critics(mode).values())
        self.sources = [load_tensor_from_image(s, device="cpu") for s in sources]

    def prepare_critics(self, app, scale):
        layers = [c.get_layers() for c in self.critics]
        sources = [
            F.interpolate(
                img,
                scale_factor=1.0 / scale,
                mode="area",
                recompute_scale_factor=False,
            ).to(device=app.device, dtype=app.precision)
            for img in self.sources
        ]

        # Combine all features into a single dictionary.
        features = [dict(app.encoder.extract(f, layers)) for f in sources]
        features = dict(zip(features[0].keys(), zip(*[f.values() for f in features])))
        
        # Initialize the critics from the combined dictionary.
        for critic in self.critics:
            critic.from_features(features)

        return [self.critics]

    def prepare_seed_tensor(self, app, size, previous=None):
        if previous is None:
            b, _, h, w = size
            mean = (
                torch.cat(self.sources, dim=0)
                .mean(dim=(0, 2, 3), keepdim=True)
                .to(app.device)
            )
            result = torch.empty((b, 1, h, w), device=app.device, dtype=torch.float32)
            result = (result.normal_(std=0.1) + mean).clamp(0.0, 1.0)
            return result.to(dtype=app.precision)

        return F.interpolate(
            previous, size=size[2:], mode="bicubic", align_corners=False
        ).clamp(0.0, 1.0)
