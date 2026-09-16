"""Pure, network-free logic for scripts/01_policy_tags.py — kept in its own
module so it can be unit tested without any GCP connection."""


def merge_binding(policy: dict, role: str, member: str) -> dict:
    """Add `member` to `role`'s bindings in `policy` without duplicating an
    existing entry, and without disturbing any other role's bindings.
    Mutates and returns `policy`."""
    bindings = policy.setdefault("bindings", [])
    for binding in bindings:
        if binding.get("role") == role:
            members = binding.setdefault("members", [])
            if member not in members:
                members.append(member)
            return policy
    bindings.append({"role": role, "members": [member]})
    return policy
