"""
Two Fifteen (twofifteen.co.uk) POD integration for KLH Autographs.

Phase 1 scope: a signed API client (`client.TwoFifteenClient`) and a set
of schema constants (`schema`). Higher-level order/webhook/fulfilment
logic will land here in subsequent phases.

Credentials live at ~/.klh/.env alongside the eBay ones, read via the
same pattern as `ebay_api.token_manager`.
"""

from .client import TwoFifteenClient, TwoFifteenError
from . import schema

__all__ = ["TwoFifteenClient", "TwoFifteenError", "schema"]
