"""Bounded fallback order without changing the chosen first preference."""


def prefer_next_provider(candidates: list, failed_index: int, *, billing=None) -> None:
    """After failure, move one different provider ahead of remaining siblings.

    Preserve existing category order among the alternatives. Do not add models,
    retry a consumed candidate, change billing permission or bypass cooldowns.
    """
    if failed_index + 1 >= len(candidates):
        return
    billing = billing or (lambda model: model.billing)
    next_category = billing(candidates[failed_index + 1])
    failed = candidates[failed_index].provider_id
    for index in range(failed_index + 1, len(candidates)):
        if billing(candidates[index]) != next_category:
            break
        if candidates[index].provider_id != failed:
            candidates.insert(failed_index + 1, candidates.pop(index))
            return


def reserve_provider_candidates(category: list, budget: int) -> list:
    """Keep first-choice ordering while reserving room for different providers."""
    selected, seen = [], set()
    providers = {m.provider_id for m in category}
    for model in category:
        if len(selected) >= budget:
            break
        if model.provider_id in seen and budget - len(selected) <= len(
            providers - seen
        ):
            continue
        selected.append(model)
        seen.add(model.provider_id)
    return selected
