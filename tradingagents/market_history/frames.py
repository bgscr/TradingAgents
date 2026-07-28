from __future__ import annotations

from hashlib import sha256

import pandas as pd


def normalized_frame_text(frame: pd.DataFrame) -> str:
    canonical = frame.copy()
    if "Date" in canonical:
        canonical["Date"] = pd.to_datetime(canonical["Date"]).dt.strftime("%Y-%m-%d")
    return canonical.to_csv(
        index=False,
        lineterminator="\n",
        float_format="%.17g",
    )


def normalized_frame_sha256(frame: pd.DataFrame) -> str:
    return sha256(normalized_frame_text(frame).encode("utf-8")).hexdigest()
