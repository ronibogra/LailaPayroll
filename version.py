"""Software version. The build pipeline writes build_info.py with the build number and date."""
MAJOR_MINOR = "4.2"
try:
    from build_info import BUILD, BUILT_ON  # type: ignore  # noqa: F401
except Exception:  # noqa: BLE001 - running from source
    BUILD, BUILT_ON = "dev", ""
VERSION = f"{MAJOR_MINOR}.{BUILD}"
