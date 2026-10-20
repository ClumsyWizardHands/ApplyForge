"""Feed adapter registry. Feeds discover; they never stage (SPEC §11)."""

from __future__ import annotations

from jobkit.adapters.feeds.a16z import A16zFeed
from jobkit.adapters.feeds.adzuna import AdzunaFeed
from jobkit.adapters.feeds.arbeitnow import ArbeitnowFeed
from jobkit.adapters.feeds.base import FeedAdapter
from jobkit.adapters.feeds.consider import ConsiderFeed
from jobkit.adapters.feeds.cosnetwork import CosNetworkFeed
from jobkit.adapters.feeds.getro import GetroFeed
from jobkit.adapters.feeds.himalayas import HimalayasFeed
from jobkit.adapters.feeds.remoteok import RemoteOKFeed
from jobkit.adapters.feeds.remotive import RemotiveFeed
from jobkit.adapters.feeds.usajobs import USAJobsFeed

FEEDS: dict[str, FeedAdapter] = {
    f.source_name: f
    for f in (
        ArbeitnowFeed(),
        RemotiveFeed(),
        RemoteOKFeed(),
        HimalayasFeed(),
        USAJobsFeed(),
        AdzunaFeed(),
        GetroFeed(),
        ConsiderFeed(),
        CosNetworkFeed(),
        A16zFeed(),
    )
}
