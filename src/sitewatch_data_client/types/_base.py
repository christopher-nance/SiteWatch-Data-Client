"""Base Pydantic model for all SiteWatch result types."""

from pydantic import BaseModel, ConfigDict


class SiteWatchModel(BaseModel):
    """Base class for every result model."""

    model_config = ConfigDict(populate_by_name=True)
