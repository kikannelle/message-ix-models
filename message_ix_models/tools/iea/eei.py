"""Retrieve data from the IEA Energy Efficiency Indicators source."""
# FIXME This file is currently excluded from coverage measurement. See
#       iiasa/message-ix-models#164

import logging
import re
from typing import TYPE_CHECKING, Literal

import genno
import numpy as np
import pandas as pd
import plotnine as p9

from message_ix_models import Context
from message_ix_models.tools.exo_data import ExoDataSource, register_source
from message_ix_models.util import cached, private_data_path

if TYPE_CHECKING:
    from genno import Computer

log = logging.getLogger(__name__)

#: Sheets in the input file.
#: Mapping of weights to variables used as weights for weighted averaging.
#:
#: .. todo:: Remove this, replace with tests showing usage of wavg()
WAVG_MAP = {
    "Fuel intensity": "vehicle-kilometres",
    "Passenger load factor": "vehicle-kilometres",
    "Vehicle use": "vehicle stock",
    "Vehicle-kilometres energy intensity": "vehicle-kilometres",
    "Freight load factor": "vehicle-kilometres",
    # Rest of variables are Weighted with population:
    # "Passenger-kilometres per capita"
    # "Per capita energy intensity"
    # "Vehicle-kilometres per capita"
    # "Tonne-kilometres per capita"
    # TODO: fix intensities, should not be weighted with population:
    # "Tonne-kilometres energy intensity": np.average,
    # "Passenger-kilometres energy intensity": np.average,
}


@register_source
class IEA_EEI(ExoDataSource):
    """Read 2020 Energy Efficiency Indicators data.

    The data is read from :data:`.FILE`.

    .. todo:: currently, the function returns a mix of aggregated and un-aggregated
    data. Add an argument and modify to consistently return one or the other.

    Parameters
    ----------
    regions : str
        Regional aggregation to use.
    plot : bool, optional
        If ``True``, plots per mode will be generated in :file:`./debug/` directory,
        and the processed data exported to :file:`eei_data_wrapped.csv`.

    Returns
    -------
    data : dict of (str -> :class:`pandas.DataFrame`)
        Keys are measures. Values are data frames with the dimensions "region", "year",
        "Mode/vehicle type"; additionally "ISO_code" (country code) and "units".
    """

    id = "IEA EEI"

    def __init__(self, source, source_kw):
        if source != self.id:
            raise ValueError(source)

        indicator = source_kw.pop("indicator", None)

        self.aggregate = source_kw.pop("aggregate", False)
        self.broadcast_map = source_kw.pop("broadcast_map", None)
        self.plot = source_kw.pop("plot", False)

        self.raise_on_extra_kw(source_kw)

        self.path = private_data_path(
            "transport", "Energyefficiencyindicators_2020-extended.xlsx"
        )
        if not self.path.exists():
            log.error(f"Not found: {self.path}")
            raise ValueError(self.path)

        # Prepare query
        self.query = f"INDICATOR == {indicator!r}"
        self.measure = "INDICATOR"
        self.name = indicator.lower()

    def __call__(self):
        from genno.operator import unique_units_from_dim

        tmp = (
            iea_eei_data_raw(self.path)
            .query(self.query)
            .rename(columns={"TIME_PERIOD": "y"})
        )

        # Identify dimensions
        # - Not the "value" or measure columns.
        # - Not columns filled entirely with "__NA".
        dims = [
            c
            for c, s in tmp.items()
            if (c not in {"value", self.measure} and set(s.unique()) != {"__NA"})
        ]

        return genno.Quantity(tmp.set_index(dims)["value"]).pipe(
            unique_units_from_dim, dim="UNIT_MEASURE"
        )

    def transform(self, c: "Computer", base_key: genno.Key) -> genno.Key:
        k = base_key

        # Aggregate
        if self.aggregate:
            k = c.add(k + "1", "aggregate", k, "n::groups", keep=False)

        if self.broadcast_map:
            k_map = genno.Key(self.broadcast_map)
            rename = {k_map.dims[1]: k_map.dims[0]}
            k = c.add(k + "2", "broadcast_map", k, self.broadcast_map, rename=rename)

        if self.plot:
            # Path for debug output
            context: "Context" = c.graph["context"]
            debug_path = context.get_local_path("debug")
            debug_path.mkdir(parents=True, exist_ok=True)
            c.configure(output_dir=debug_path)

            c.add(f"plot {self.id} debug", Plot, k)

        # Specific handling for different sheets
        name = df = dfs = None
        if name == "indicators":
            # Apply wavg() to each measure; store results
            for var, group_df in df.groupby("variable"):
                dfs[var.lower()] = wavg(var, group_df, dfs)

        return k


class Plot(genno.compat.plotnine.Plot):
    """Plot values from file."""

    """Generate ggplot object populated with EEIs per transport mode."""

    basename = "IEA_EEI-data"

    static = [
        p9.aes(x="Year", y="Value", color="region"),
        p9.geom_line(),
        p9.facet_wrap("Variable", scales="free_y"),
        p9.labs(x="Year", y="mode"),
        p9.theme(subplots_adjust={"wspace": 0.15}, figure_size=(11, 9)),
    ]

    def generate(self, data):
        for mode, group_df in data.groupby("Mode/vehicle type"):
            yield p9.ggplot(group_df) + self.static + p9.ggtitle(mode)


SECTOR_MEASURE_EXPR = re.compile(r"(?P<SECTOR>[^ -]+)[ -](?P<MEASURE0>.+)")
MEASURE_UNIT_EXPR = re.compile(r"(?P<MEASURE1>.+) \((?P<UNIT_MEASURE>.+)\)")


def extract_measure_and_units(df: pd.DataFrame) -> pd.DataFrame:
    # Identify the column containing a units expression: either "Indicator" or "Product"
    measure_unit_col = ({"Indicator", "Product"} & set(df.columns)).pop()
    # - Split the identified column to UNIT_MEASURE and either INDICATOR or PRODUCT.
    # - Concatenate with the other columns.
    return pd.concat(
        [
            df.drop(measure_unit_col, axis=1),
            df[measure_unit_col]
            .str.extract(MEASURE_UNIT_EXPR)
            .rename(columns={"MEASURE1": measure_unit_col.upper()}),
        ],
        axis=1,
    )


def melt(df: pd.DataFrame) -> pd.DataFrame:
    """Melt on any dimensions."""
    index_cols = set(df.columns) & {
        "Activity",
        "Country",
        "End use",
        "INDICATOR",
        "MEASURE",
        "Mode/vehicle type",
        "PRODUCT",
        "SECTOR",
        "Subsector",
        "UNIT_MEASURE",
    }
    return df.melt(id_vars=sorted(index_cols), var_name="TIME_PERIOD")


@cached
def iea_eei_data_raw(path, non_iso_3166: Literal["keep", "discard"] = "discard"):
    from message_ix_models.util.pycountry import iso_3166_alpha_3

    xf = pd.ExcelFile(path)

    dfs = []
    for sheet_name in xf.sheet_names:
        # Parse the sheet name
        match = SECTOR_MEASURE_EXPR.fullmatch(sheet_name)
        if match is None:
            continue

        # Preserve the sector and/or measure ID from the sheet name
        s, m = match.groups()
        assign = dict()
        if s not in ("Activity",):
            assign.update(SECTOR=s.lower())
        if m in ("Energy", "Emissions"):
            assign.update(MEASURE=m.lower())

        # - Read the sheet.
        # - Drop rows containing only null values.
        # - Right-strip whitespaces from columns containing strings.
        # - Assign sector and/or measure ID.
        # - Extract units.
        # - Melt from wide to long layout.
        # - Drop null values.
        df = (
            xf.parse(sheet_name, header=1, na_values="..")
            .dropna(how="all")
            .apply(lambda col: col.str.rstrip() if col.dtype == object else col)
            .assign(**assign)
            .pipe(extract_measure_and_units)
            .pipe(melt)
            .dropna(subset="value")
        )
        assert not df.isna().any(axis=None)
        dfs.append(df)

    return (
        pd.concat(dfs)
        .fillna("__NA")
        .assign(n=lambda df: df["Country"].apply(iso_3166_alpha_3))
        .drop("Country", axis=1)
    )


def wavg(measure: str, df: pd.DataFrame, weight_data: pd.DataFrame) -> pd.DataFrame:
    """Perform masked & weighted average for `measure` in `df`, using `weight_data`.

    .. todo:: Replace this with usage of genno; add tests.

    :data:`.WAVG_MAP` is used to select a data from `weight_data` appropriate for
    weighting `measure`: either "population", "vehicle stock" or "vehicle-kilometres*.
    If the measure to be used for weights is all NaNs, then "population" is used as a
    fallback as weight.

    The weighted average is performed by grouping `df` on the "region", "year", and
    "Mode/vehicle type" dimensions, i.e. the values returned are averages weighted
    within these groups.

    Parameters
    ----------
    measure : str
        Name of measure contained in `df`.
    df : pandas.DataFrame
        Data to be aggregated.
    weight_data : pandas.DataFrame.
        Data source for weights.

    Returns
    -------
    pandas.DataFrame
    """
    # Choose the measure for weights using `WAVG_MAP`.
    weights = WAVG_MAP.get(measure, "population")
    if weight_data[weights]["value"].isna().all():
        # If variable to be used for weights is all NaNs, then use population as weights
        # since pop data is available in all cases
        weights = "population"

    # Align the data and the weights into a single data frame
    id_cols = ["region", "year", "Mode/vehicle type"]
    data = df.merge(
        weight_data[weights],
        on=list(filter(lambda c: c in weight_data[weights].columns, id_cols)),
    )

    units = data["units_x"].unique()
    assert 1 == len(units), units

    def _wavg(group):
        # Create masked arrays, masking NaNs from the weighted average computation:
        d = np.ma.masked_invalid(group["value_x"].values)
        w = np.ma.masked_invalid(group["value_y"].values)
        # Compute weighted average
        return np.ma.average(d, weights=w)

    # - Apply _wavg() to groups by `id_cols`.
    # - Return to a data frame.
    # - Re-insert "units" and "variable" columns.
    return (
        data.groupby(id_cols)
        .apply(_wavg)
        .rename("value")
        .reset_index()
        .assign(units=units[0], variable=measure)
    )
