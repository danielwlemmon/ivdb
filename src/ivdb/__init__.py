"""ivdb: daily implied-volatility collector built on CBOE delayed option quotes.

Units contract (see docs/schema.md): every implied volatility stored by this
package is an annualized DECIMAL (0.2053 == 20.53%). Ranks/percentiles printed
by `ivdb ivr` are 0-100.
"""

__version__ = "0.1.0"
