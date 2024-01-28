"""
translate all params from the tools.yml and input.json into a pydantic model
this will make access to the variables easier across many submodules

This could be a general pattern for json2args. We would need a factory function
that consumes the yml to build the model and uses the inputs.json to instantiate it
"""
from typing import List
from datetime import datetime
from pathlib import Path
import tempfile
from enum import Enum

from pydantic import BaseModel, Field
import geopandas as gpd


# create the Enum for integration type
class Integrations(str, Enum):
    TEMPORAL = 'temporal'
    SPATIAL = 'spatial'
    ALL = 'all'
    NONE = 'none'


class NetCDFBackends(str, Enum):
    XARRAY = 'xarray'
    CDO = 'cdo'
    PARQUET = 'parquet'


class Params(BaseModel):
    dataset_ids: List[int]
    reference_area: dict = Field(repr=False)

    start_date: datetime = None
    end_date: datetime = None
    keep_intermediate: bool = False
    integration: Integrations = Integrations.ALL

    # stuff that we do not change in the tool
    base_path: str = '/out'
    netcdf_backend: NetCDFBackends = NetCDFBackends.XARRAY

    @property
    def intermediate_path(self) -> Path:
        if self.keep_intermediate:
            p = Path(self.base_path) / 'intermediate'
        else:
            p = Path(tempfile.mkdtemp())
        
        # make the directory if it does not exist
        p.mkdir(parents=True, exist_ok=True)

        # return the path
        return p
    
    @property
    def dataset_path(self) -> Path:
        return Path(self.base_path) / 'datasets'
    
    @property
    def reference_area_df(self) -> gpd.GeoDataFrame:
        return gpd.GeoDataFrame.from_features([self.reference_area])

# manage a single instance to this class
__SINGLETON: Params = None
def load_params(**kwargs) -> Params:
    global __SINGLETON
    # create if needed
    if __SINGLETON is None:
        __SINGLETON = Params(**kwargs)
    
    # return
    return __SINGLETON