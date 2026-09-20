from free_claude_code.application.free_pool import FreeModel


def model(provider="open_router", name="free-big", context=1048576, vision=False):
    return FreeModel(provider, name, context, 8192, True, vision, "synthetic")


def freeze_pool(pool, models):
    pool._catalog = tuple(models)

    async def refresh(settings, *, force=False):
        pass

    pool.refresh = refresh
    return pool
