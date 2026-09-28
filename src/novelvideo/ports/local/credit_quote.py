"""Local CE generation credit quote implementation."""

from __future__ import annotations

import logging
import math

from novelvideo.ports.credit_quote import CreditQuote

logger = logging.getLogger(__name__)

_FREE = CreditQuote(total_cost=0, display="0", unit="call", unit_cost=0, quantity=1, params={})


def _video_request(kind: str, model: str, params: dict, quantity: int) -> dict | None:
    """-> {backend, calls, duration, resolution, generate_audio} for a video quote."""
    if kind == "video":
        return {
            "backend": model,
            "calls": 1,
            "duration": max(quantity, 1),
            "resolution": params.get("resolution"),
            "generate_audio": None,
        }
    if kind == "feature" and params.get("pricing_kind") == "video":
        metrics = params.get("pricing_metrics") or {}
        calls = max(int(metrics.get("call_count") or 1), 1)
        return {
            "backend": str(params.get("pricing_model") or ""),
            "calls": calls,
            "duration": max(int(metrics.get("output_duration_seconds") or 0) / calls, 1),
            "resolution": (params.get("pricing_params") or {}).get("resolution"),
            "generate_audio": params.get("generate_audio"),
        }
    return None


async def _image_credits(model: str, params: dict) -> int:
    """Higgsfield's own price (`higgsfield generate cost`); Draw Things is free."""
    from novelvideo.engines.image import is_selection, quote_credits

    selection = model if is_selection(model) else f"higgsfield:{model}"
    try:
        credits = await quote_credits(
            selection,
            image_size=str(params.get("size") or "") or None,
            quality=str(params.get("quality") or "") or None,
        )
    except Exception as exc:  # noqa: BLE001 - an offline engine quotes 0, never 500s
        logger.info("image quote unavailable for %s: %s", selection, exc)
        return 0
    return math.ceil(credits)


class LocalCreditQuote:
    """CE bills nothing itself; Higgsfield images and videos show Higgsfield's own price."""

    async def generation_credit_quote(
        self,
        *,
        kind: str,
        model: str,
        params: dict,
        quantity: int,
        product_surface: str,
        user_id: str = "",
    ) -> CreditQuote:
        del product_surface, user_id
        if kind == "image":
            unit_cost = 0
            if model and model != "drawthings":
                unit_cost = await _image_credits(model, params or {})
            total = unit_cost * max(int(quantity or 1), 1)
            return CreditQuote(
                total_cost=total,
                display=str(total),
                unit="call",
                unit_cost=unit_cost,
                quantity=1,
                params={},
            )
        request = _video_request(kind, model, params, quantity)
        if request is None:
            return _FREE
        from novelvideo.engines._proc import EngineError
        from novelvideo.generators.video_generator import video_quote

        calls = request["calls"]
        try:
            credits = await video_quote(
                request["backend"],
                duration=request["duration"],
                resolution=request["resolution"],
                generate_audio=request["generate_audio"],
            )
        except (EngineError, OSError, ValueError) as exc:
            logger.warning("video quote unavailable for %s: %s", request["backend"], exc)
            return _FREE
        total = math.ceil(credits * calls)
        return CreditQuote(
            total_cost=total,
            display=f"{credits * calls:g}",
            unit="call",
            unit_cost=math.ceil(credits),
            quantity=calls,
            params={},
        )
