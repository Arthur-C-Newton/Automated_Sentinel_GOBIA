# This is the main script
# the docstring should go here

# import the required modules
from typing import Tuple
from zipfile import ZipFile
import os
from pathlib import Path
import rasterio
from rasterio import mask
import geopandas as gpd
import gdal
from rsgislib.segmentation import segutils
import rsgislib.rastergis
import rsgislib.rastergis.ratutils
import rsgislib.classification.classratutils
from sklearn.model_selection import GridSearchCV
from sklearn.ensemble import RandomForestClassifier
from numpy import random
import csv
import argparse

# define command line parser arguments
parser = argparse.ArgumentParser()
parser.add_argument("-i", "--input_path", help="path to input folder", default=".\\input")
parser.add_argument("-o", "--output_path", help="path to final output folder", default=".\\output")
parser.add_argument("-t", "--tmp_path", help="path to temp folder", default=".\\tmp")
parser.add_argument("-zp", "--zip_path", help="file path to zip file containing image bands", default="None")
parser.add_argument("-ex", "--extent", help="file path to shapefile extent", default="None")
parser.add_argument("-tr", "--training_data", help="file path to training data shapefile", default="None")
parser.add_argument("-st", "--stack_out", help="file output path for stacked multiband tif", default="None")
parser.add_argument("-cl", "--class_col", help="column in training data containing class names", default="Ecological")
parser.add_argument("-v", "--validate", help="split training data and perform validation", action="store_true")
args = parser.parse_args()


def fix_paths(arg, folder="None", name="None"):
    """
    Assigns file path from parser argument to a variable, including cases where two arguments are required.

        Parameters:
            arg (str): Parser argument to be assigned
            folder (str): Parser argument for the containing folder (if required)
            name (str): Name of the file (if required)

        Returns:
            var (str): Variable containing file path
    """
    if arg == "None":
        var = folder + "\\" + name
    else:
        var = arg
    return var


def band_index(band, file_list):
    """
    Find the index of the required band in a file list of sentinel single-band images

    :param band: Band number
    :param file_list: List of Sentinel single-band image files
    :return idx_name: Index of band image in file list
    """
    if band <10:
        band_suffix = "_B" + "0" + str(band)
    else:
        band_suffix = "_B" + str(band)
    idx_name = [i for i, s in enumerate(file_list) if band_suffix in s]
    return idx_name[0]


def stack():
    """Import single-band images from downloaded zip file and stack into one multi-band tif file"""

    # create ist of files that are band images
    archive = ZipFile(zip_path, 'r')
    files = [name for name in archive.namelist() if name.endswith('.jp2') and '_B' in name]

    # create a list of only the desired bands
    indices = [band_index(2, files), band_index(3, files), band_index(4, files), band_index(8, files)]
    bands = [files[i] for i in indices]  # The original band numbers are not preserved

    # read the metadata for the first image
    band2 = rasterio.open("zip:" + zip_path + "!" + files[band_index(2, files)])
    meta = band2.meta
    meta.update(count=len(bands), driver="GTiff")  # update the metadata to allow multiple bands

    # clip to extent if one is provided
    if os.path.exists(extent_path):
        extent = gpd.read_file(extent_path)
        shapes = extent["geometry"]
        clipped_image, clipped_transform = mask.mask(band2, shapes, crop=True)
        clipped_meta = meta
        clipped_meta.update({"driver": "GTiff",
                             "height": clipped_image.shape[1],
                             "width": clipped_image.shape[2],
                             "transform": clipped_transform})
        # create a single stacked geotiff based on the image metadata
        with rasterio.open(stack_path, 'w', **clipped_meta) as dst:
            for id, layer in enumerate(bands, start=1):
                with rasterio.open("zip:" + zip_path + "!" + layer) as src1:
                    out_image, transform = mask.mask(src1, shapes, crop=True)
                    dst.write_band(id, out_image[0])
                    print("Writing band...")
    else:  # if no extent is provided, the full image is staved to disk (very slow)
        # create a single stacked geotiff based on the image metadata
        with rasterio.open(stack_path, 'w', **meta) as dst:
            for id, layer in enumerate(bands, start=1):
                with rasterio.open("zip:" + zip_path + "!" + layer) as src1:
                    dst.write_band(id, src1.read(1))
                    print("Writing band...")
    print("Multi-band GeoTiff saved successfully at " + stack_path)


def segment(multiband):
    """
    Run segmentation on multi-band raster

    :param multiband: path to multi-band raster image
    :return clumps: segmented raster attribute table containing statistics
    """
    # re-import data and save as KEA
    raster = gdal.Open(multiband)
    gdal.Translate("tmp\\raster.kea", raster, format="KEA")

    in_img = "tmp\\raster.kea"
    clumps = "clumps_image.kea"

    print("Running segmentation...")

    # segment the image using rsgislib
    segutils.runShepherdSegmentation(in_img, clumps, tmpath=tmp_path, numClusters=100, minPxls=100, distThres=100,
                                     sampling=100, kmMaxIter=200)
    band_info = []
    band_info.append(rsgislib.rastergis.BandAttStats(
        band=1, minField='BlueMin', maxField='BlueMax',
        meanField='BlueMean', stdDevField='BlueStdev'))
    band_info.append(rsgislib.rastergis.BandAttStats(
        band=2, minField='GreenMin', maxField='GreenMax',
        meanField='GreenMean', stdDevField='GreenStdev'))
    band_info.append(rsgislib.rastergis.BandAttStats(
        band=3, minField='RedMin', maxField='RedMax',
        meanField='RedMean', stdDevField='RedStdev'))
    band_info.append(rsgislib.rastergis.BandAttStats(
        band=4, minField='NIRMin', maxField='NIRMax',
        meanField='NIRMean', stdDevField='NIRStdev'))
    rsgislib.rastergis.populateRATWithStats(in_img, clumps, band_info)
    return clumps


def training_prep(split):
    """Preprocess training data"""

    # import training data from shapefile
    training_data = gpd.read_file(shp_path)
    class_col_name = args.class_col  # assign the class name column to a variable

    # save each land cover class as a separate shapefile
    if split:
        for l_class, training_class in training_data.groupby(class_col_name):
            shp_train = training_class.sample(frac=0.7)
            shp_test = training_class.drop(shp_train.index())
            shp_train.to_file(tmp_path + "\\" + l_class + "_train" + ".shp")
            shp_test.to_file(tmp_path + "\\" + l_class + "_test" + ".shp")
    else:
        for l_class, training_class in training_data.groupby(class_col_name):
            training_class.to_file(tmp_path + "\\" + l_class + ".shp")

    # read shapefiles of each class into dictionary and assign colour values to them for classification
    class_dict = dict()
    index = 0
    class_colours = dict()
    colour = [0, 0, 0]
    for file in os.listdir(tmp_path):
        if file.endswith(".shp") and "_test" not in file:
            class_path = os.path.join(tmp_path, file)
            class_filename = Path(class_path).stem
            class_name_nospace = class_filename.replace(" ", "_")
            index += 1
            class_dict[class_name_nospace] = [index, class_path]
            random.seed(index)
            colour = random.randint(255, size=3)
            class_colours[class_name_nospace] = colour


# assign file paths from parser arguments to variables
extent_path = fix_paths(args.extent, args.input_path, "extent.shp")
shp_path = fix_paths(args.training_data, args.input_path, "training_data.shp")
in_path = fix_paths(args.input_path)
out_path = fix_paths(args.output_path)
tmp_path = fix_paths(args.tmp_path)
stack_path = fix_paths(args.stack_out, args.tmp_path, "stack.tif")

# retrieve path to zip file in input folder if full path to image zip file not provided
if args.zip_path == "None":
    for file in os.listdir(in_path):
        if file.endswith(".zip"):
            zip_path = os.path.join(in_path, file)
else:
    zip_path = args.zip_path

stack()
segment(stack_path)
training_prep(args.validate)



# populate segments with training data
class_int_col_in = "ClassInt"
class_name_col = "ClassStr"
rsgislib.rastergis.ratutils.populateClumpsWithClassTraining(clumps, class_dict, tmp_path, class_int_col_in,
                                                            class_name_col)

# find the optimal parameters for the classifier
variables = ['BlueMean', 'GreenMean', 'RedMean', 'NIRMean']
grid_search = GridSearchCV(RandomForestClassifier(),
                           param_grid={'n_estimators': [10, 20, 50, 100], 'max_depth': [2, 4, 8]})
classifier = rsgislib.classification.classratutils.findClassifierParameters(clumps, class_int_col_in, variables,
                                                                            gridSearch=grid_search)

# apply the classifier to the image
out_class_int_col = 'OutClass'
out_class_str_col = 'OUtClassName'
rsgislib.classification.classratutils.classifyWithinRATTiled(clumps, class_int_col_in, class_name_col, variables,
                                                             classifier=classifier, outColInt=out_class_int_col,
                                                             outColStr=out_class_str_col, classColours=class_colours)

# export classified image to a GeoTiff
filename = Path(zip_path).stem
datatype = rsgislib.TYPE_8INT
out_class_img = out_path + "\\" + filename + "_classified.tif"
rsgislib.rastergis.exportCol2GDALImage(clumps, out_class_img, "GTiff", datatype, out_class_int_col)

# save the image values and the classes they represent to a csv file
# values are retrieved from the class dictionary (includes classes not present in final image)
class_values = {}
for i, key in enumerate(class_dict):
    class_values[key] = list(class_dict.keys()).index(key) + 1
with open(out_path + "\\classes.csv", 'w') as f:
    w = csv.writer(f)
    w.writerows(class_values.items())
