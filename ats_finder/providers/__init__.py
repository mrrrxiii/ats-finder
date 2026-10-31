from ats_finder.providers.ashby import AshbyAdapter
from ats_finder.providers.base import FetchResult, ProviderAdapter, ProviderError
from ats_finder.providers.greenhouse import GreenhouseAdapter
from ats_finder.providers.lever import LeverAdapter
from ats_finder.providers.smartrecruiters import SmartRecruitersAdapter
from ats_finder.providers.workable import WorkableAdapter


ADAPTERS: dict[str, ProviderAdapter] = {
    "greenhouse": GreenhouseAdapter(),
    "lever": LeverAdapter(),
    "ashby": AshbyAdapter(),
    "smartrecruiters": SmartRecruitersAdapter(),
    "workable": WorkableAdapter(),
}

__all__ = [
    "ADAPTERS",
    "AshbyAdapter",
    "FetchResult",
    "GreenhouseAdapter",
    "LeverAdapter",
    "SmartRecruitersAdapter",
    "WorkableAdapter",
    "ProviderAdapter",
    "ProviderError",
]
