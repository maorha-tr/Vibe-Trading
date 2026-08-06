"""eToro connector package.

Talks to the eToro public API (``public-api.etoro.com``) over plain REST with
the ``x-api-key``/``x-user-key`` header pair. eToro exposes parallel demo
(virtual portfolio) and real trading routes on the same host; a user key is
generated for exactly one environment, so the paper boundary is enforced both
by path construction and by the credential itself. This layer ships demo
read/trade profiles plus a real read-only profile; a real-money trade profile
is intentionally not offered yet.
"""
