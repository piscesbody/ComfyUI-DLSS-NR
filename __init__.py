from comfy_api.latest import ComfyExtension, io

from .nodes import DLSSNRVideoUpscale, DLSSNRImageUpscale


class DLSSNRExtension(ComfyExtension):
    async def on_load(self):
        pass

    async def get_node_list(self):
        return [DLSSNRVideoUpscale, DLSSNRImageUpscale]


def comfy_entrypoint():
    return DLSSNRExtension()


__all__ = ["comfy_entrypoint"]
