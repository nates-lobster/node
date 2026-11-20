"""API connectors: fetch sources over plain HTTP instead of always-on tabs.

See docs/connectors/README.md for the design and how to add a connector.
"""

from .base import Connector, ConnectorError, ConnectorItem, FetchResult  # noqa: F401
