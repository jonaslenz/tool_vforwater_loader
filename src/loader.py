import glob
import subprocess
import time
from concurrent.futures import Executor

from metacatalog.models import Entry
import rioxarray
import xarray as xr
import pandas as pd
import geopandas as gpd
import rasterio as rio


from logger import logger
from writer import dispatch_save_file
from param import load_params, Params



# Maybe this function becomes part of metacatalog core or a metacatalog extension
def load_entry_data(entry: Entry, executor: Executor) -> str:
    # 1. get the path to the datasource
    path_type = entry.datasource.type.name

    # if the type is internal or external, we need to use the load_sql_source
    if path_type in ('internal', 'external'):
        data_path = load_sql_source(entry, executor=executor)
    # TODO: here we can be explicite about data source types
    else:
        data_path = load_file_source(entry, executor=executor)

    # Return the data path to the entry-level dataset
    return data_path


def load_sql_source(entry: Entry, executor: Executor):
    # if the source is external, we can't use it right now, as it is not clear
    # yet if the Datasource.path is the connection or the path inside the database
    if entry.datasource.type.name == 'external':
        raise NotImplementedError("External database datasources are not supported yet.")
    
    # we are internal and can request a session to the database
    # TODO: now this has to be replaced by the new logic where every entry goes into its own table
    data = entry.get_data(start=start, end=end)

    return data


def load_http_source(entry: Entry):
    raise NotImplementedError("HTTP datasources are not supported yet.")


def load_file_source(entry: Entry, executor: Executor) -> str:
    # create a Path from the name
    name = entry.datasource.path
    path = Path(name)

    # go for the different suffixes
    if path.suffix.lower() in ('.nc', '.netcdf', '.cdf', 'nc4'):
        # laod the netCDF file time & space chunks to the output folder
        out_path = load_netcdf_file(entry, executor=executor)
        
        # return the dataset
        return out_path
    elif path.suffix.lower() in ('.tif', '.tiff', '.dem'):
        raise NotImplementedError('GeoTiff loader is currently not implemented, sorry.')


def load_netcdf_file(entry: Entry, executor: Executor) -> str:
    # load the params
    params = load_params()

    # get the file name
    name = entry.datasource.path

    # check if there is a wildcard in the name
    if '*' in name:
        fnames = glob.glob(name)
    else:
        fnames = [name]
    
    # check the amount of files to be processed
    if len(fnames) > 1:
        logger.debug(f"For {name} found {len(fnames)} files.")
    elif len(fnames) == 0:
        logger.warning(f"Could not find any files for {name}.")
        return None
    else:
        logger.debug(f"Resource {name} is single file.")

    # get the time axis
    temporal_dims = entry.datasource.temporal_scale.dimension_names if entry.datasource.temporal_scale is not None else []
    
    # get a path for the current dataset path
    dataset_base_path = params.dataset_path / f"{entry.variable.name.replace(' ', '_')}_{entry.id}"

    # preprocess each netcdf / grib / zarr file
    for fname in fnames:
        # read the min and max time and check if we can skip
        ds = xr.open_dataset(fname, decode_coords='all', mask_and_scale=True)

        # check if we there is a time axis
        if len(temporal_dims) > 0:
            # get the min and max time
            min_time = pd.to_datetime(ds[temporal_dims[0]].min().values)
            max_time = pd.to_datetime(ds[temporal_dims[0]].max().values)

            if (
                params.start_date is not None and params.start_date > max_time.tz_localize(params.start_date.tzinfo)
            ) or (
                params.end_date is not None and params.end_date < min_time.tz_localize(params.end_date.tzinfo)
            ):
                logger.debug(f'skipping {fname} as it is not in the time range: {params.start_date} - {params.end_date}')
                ds.close()
                continue
        else:
            ds.close()
            logger.warning(f"The dataset {fname} does not contain a datetime coordinate.")
        
        # 
        if params.netcdf_backend == 'cdo':
            ds.close()
            path = _clip_netcdf_cdo(fname, params)

            #TODO to the mergetime here
            pass

            return path
        elif params.netcdf_backend == 'xarray':
            data = _clip_netcdf_xarray(entry, ds, params)
        elif params.netcdf_backend == 'parquet':
            # use the xarray clip first
            ds = _clip_netcdf_xarray(entry, ds, params)

            data = ds.to_dask_dataframe()[entry.datasource.dimension_names].dropna()
        
        # if we are still here, dispatch the save task for intermediate file chunk
        # we do not need the future here, we can directly move to the next file
        dispatch_save_file(entry=entry, data=data, executor=executor, base_path=str(dataset_base_path))

    # return the out_path
    return str(dataset_base_path)

def _clip_netcdf_cdo(path: Path, params: Params):
    # get the output name
    out_name = params.intermediate_path / path.name

    # build the several commands
    ref = params.reference_area_df
    bnd = ref.geometry[0].bounds

    # create the lonlatbox command
    lonlat_cmd = f"-sellonlatbox,{bnd[0]},{bnd[2]},{bnd[1]},{bnd[3]}"

    # create the selregion command
    selregion_cmd = f"-selregion,{params.base_path}/reference_area.ascii"

    # build the full command
    cmd = ['cdo', selregion_cmd, lonlat_cmd, str(path), str(out_name)]
    
    # run
    t1 = time.time()
    subprocess.run(cmd)
    t2 = time.time()

    # log the command
    logger.info(' '.join(cmd))
    logger.info(f"took {t2-t1:.2f} seconds")
    
    return str(out_name)

def _clip_netcdf_xarray(entry: Entry, data: xr.Dataset, params: Params):
    if data.rio.crs is None:
        logger.error(f"Could not clip {data} as it has no CRS.")
        
        # TODO: how to handle this case?
        return data
    
    # TODO: log the stuff we are doing here!
    # extract only the data variable
    ds = data[entry.datasource.variable_names].copy()

    # first go for the lonlatbox clip
    ref = params.reference_area_df
    bounds = ref.geometry[0].bounds

    # then the region clip
    if entry.datasource.temporal_scale is not None:
        ds.chunk({entry.datasource.temporal_scale.dimension_names[0]: 1})
    
    # do the lonlat and then the region clip
    lonlatbox = ds.rio.clip_box(*bounds, crs=4326)
    region = lonlatbox.rio.clip([ref.geometry[0]], crs=4326)

    # do the time clip
    if entry.datasource.temporal_scale is not None:
        time_slice = slice(params.start_date, params.end_date)
        region = region.sel(**{entry.datasource.temporal_scale.dimension_names[0]: time_slice})

    # return the new dataset
    return region



def load_raster_file(entry: Entry, name: str, reference_area: dict, base_path: str = '/out') -> rio.DatasetReader:
    #DAS hier passt noch nicht zum workflow
    #Eher alle load Funktionen dispatchen? not sure
    # build a GeoDataFrame from the reference area
    df = gpd.GeoDataFrame.from_features([reference_area])

    # open the raster file using rasterio
    if '*' in name:
        fnames = glob.glob(name)
    else:
        fnames = [name]
    
    # check the amount of files to be processed
    if len(fnames) > 1:
        logger.debug(f"For {name} found {len(fnames)} files.")
    elif len(fnames) == 0:
        logger.warning(f"Could not find any files for {name}.")
        return None
    else:
        logger.debug(f"Resource {name} is single file.")

    # preprocess each file
    for fname in fnames:
        t1 = time.time()
        with rio.open(fname, 'r') as src:
            # do the mask
            
            out_raster, out_transform = rio.mask.mask(src, [df.geometry.values], crop=True)

            # save the masked raster to the output folder
            out_meta = src.meta.copy()
        
        # update the metadata
        out_meta.update({
            "height": out_raster.shape[1],
            "width": out_raster.shape[2],
            "transform": out_transform
        })

        # save the raster
        out_path = Path(base_path) / Path(fname).name
        with rio.open(str(out_path), 'w', **out_meta) as dst:
            dst.write(out_raster)
    

# deprecated
# we do not merge them anymore            
def merge_multi_file_netcdf(entry: Entry, path: str, save_nc: bool = True, save_parquet: bool = True) -> pd.DataFrame:
    # check if this file should be saved
    if save_nc:
        out_name = f'/out/{entry.variable.name.replace(" ", "_")}_{entry.id}_lonlatbox.nc'
    else:
        out_name = f'{path}/merged_lonlatbox.nc'

    # build the CDO command
    merge_cmd = ['cdo', 'mergetime', str(Path(path) / '*.nc'), out_name]
    
    # run merge command
    t1 = time.time()
    subprocess.run(merge_cmd)
    t2 = time.time()
    logger.info(' '.join(merge_cmd))
    logger.info(f"took {t2-t1:.2f} seconds")

    # open the merged data
    # TODO infer time_axis from the entry and figure out a useful time_axis chunk size here
    data = xr.open_dataset(out_name, decode_coords=True, mask_and_scale=True, chunks={'time': 1})

    if not save_parquet:
        return data
    
    # TODO: put this into an extra STEP
    # TODO: figure out axis_names from the entry here THIS IS NOT REALLY USEFULL
    time_axis = next(([_] for _ in ('tstamp', 'time', 'date', 'datetime') if _ in data.coords), [])
    x_axis = next(([_] for _ in ('lon', 'longitude',  'x') if _ in data.coords), [])
    y_axis = next(([_] for _ in ('lat', 'latitude', 'y') if _ in data.coords), [])
    var_name = [_ for _ in ('pr', 'hurs', 'tas', 'rsds', 'tasmin', 'tasmax') if _ in data.data_vars]
    variable_names = [*time_axis, *x_axis, *y_axis, *var_name]

    # convert to long format
    t1 = time.time()
    df = data[var_name].to_dask_dataframe()[variable_names]
    t2 = time.time()
    logger.debug(f"Converting {out_name} to long format in {t2-t1:.2f} seconds.")

    return df


