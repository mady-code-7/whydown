"""URL validation and SSRF filter for WhyDown.

To be implemented in Phase 5: Security hardening.

Implements the security considerations from spec section 12:
- Block private/loopback/link-local addresses after DNS resolution
- Allow only http/https schemes
- Limit sites, response size, redirects, and timeouts
"""