# SecurityContext Handling Fix

## What was changed

Updated `instantiate_kcro.py` to avoid crashing when `securityContext` is not a dictionary.

### Fixed locations

- In `_workload()` when processing `podspec`:
  - Replaced `psc = podspec.get("securityContext") or {}` with a type-safe guard.
  - Now `psc = podspec.get("securityContext")` is followed by:
    ```python
    if not isinstance(psc, dict):
        psc = {}
    ```
  - This prevents `AttributeError: 'str' object has no attribute 'get'` when `securityContext` is a string or any non-dict.

- In `_workload()` when processing each container:
  - Replaced `csc = c.get("securityContext") or {}` with a type-safe guard.
  - Now `csc = c.get("securityContext")` is followed by:
    ```python
    if not isinstance(csc, dict):
        csc = {}
    ```
  - This ensures container securityContext processing is safe for malformed values.

## Why this fixes the error

The code previously assumed `securityContext` was always a dictionary and used `.get()` directly.
When the YAML data contained a string or another scalar value at `securityContext`, Python raised an `AttributeError`.

The fix normalizes invalid `securityContext` values to `{}` before accessing keys.

## Result

The script now skips malformed `securityContext` values gracefully, allowing the KCRO graph generation to continue.
