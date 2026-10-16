"""Failure classification functions for WhyDown.

To be implemented in Phase 2: Checker core.
"""

from typing import Optional, Tuple


def classify_result(dns_success: bool, tcp_success: bool, tls_success: bool,
                   http_success: bool, status_code: Optional[int],
                   ttfb_ms: Optional[float], slow_threshold_ms: int,
                   keyword_found: Optional[bool] = None) -> Tuple[str, Optional[str]]:
    """Classify the result of a check into a status code and failed stage.

    Returns:
        Tuple of (result_code, failed_stage)
        - result_code: One of the classification codes from spec section 8.3
        - failed_stage: One of 'dns', 'tcp', 'tls', 'http', or None if OK/SLOW
    """
    # Placeholder implementation - to be completed in Phase 2
    return "OK", None