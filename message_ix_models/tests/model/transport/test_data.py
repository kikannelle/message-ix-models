import pandas as pd
import pytest
import xarray as xr
from iam_units import registry
from message_ix import make_df
from message_ix_models.model import bare
from pandas.testing import assert_series_equal
from pytest import param


from message_data import testing
from message_data.model.transport import data as data_module
from message_data.model.transport.data.emissions import get_emissions_data
from message_data.model.transport.data.groups import (
    get_consumer_groups,
    get_urban_rural_shares,
)
from message_data.model.transport.data.ikarus import get_ikarus_data
from message_data.model.transport.data.ldv import get_ldv_data
from message_data.tools import load_data


@pytest.mark.parametrize(
    "key",
    [
        "ldv-class",
        "mer-to-ppp",
        "population-suburb-share",
        "ma3t/population",
        "ma3t/attitude",
        "ma3t/driver",
    ],
)
@pytest.mark.parametrize("rtype", (pd.Series, xr.DataArray))
def test_load_data(session_context, key, rtype):
    # Load transport metadata from files in both pandas and xarray formats
    result = load_data(session_context, "transport", key, rtype=rtype)
    assert isinstance(result, rtype)


@pytest.mark.parametrize(
    "regions",
    [
        "R11",
        param("R14", marks=testing.NIE),
        param("ISR", marks=testing.NIE),
    ],
)
def test_demand(transport_context_f, regions):
    """Test :func:`.transport.data.demand` that returns demand data."""
    ctx = transport_context_f
    ctx["regions"] = regions
    ctx["transport build info"] = info = bare.get_spec(ctx)["add"]

    # Function runs
    data = data_module.demand(ctx)

    # Returns a dict with a single key/DataFrame
    demand = data.pop("demand")
    assert 0 == len(data)

    # Demand is expressed for the expected quantities
    assert {"transport pax RUEMF", "transport pax air"} < set(demand["commodity"])

    # Demand covers the model horizon
    assert info.Y[-1] == max(
        demand["year"].unique()
    ), "`demand` does not cover the model horizon"


@pytest.mark.parametrize("years", ["A", "B"])  # param("B", marks=testing.NIE)])
@pytest.mark.parametrize("regions, N_node", [("R11", 11), ("R14", 14), ("ISR", 1)])
def test_ikarus(transport_context_f, regions, N_node, years):
    ctx = transport_context_f
    ctx.regions = regions
    ctx.years = years

    # Information about the corresponding base model
    s_info = bare.get_spec(ctx)["add"]
    ctx["transport build info"] = s_info

    # get_ikarus_data() succeeds on the bare RES
    data = get_ikarus_data(ctx)

    # Returns a mapping
    # Retrieve DataFrame for par e.g. 'inv_cost' and tech e.g. 'rail_pub'
    inv = data["inv_cost"]
    inv_rail_pub = inv[inv["technology"] == "rail_pub"]

    # Regions * 13 years (inv_cost has 'year_vtg' but not 'year_act' dim)
    rows_per_tech = N_node * (len(s_info.Y) + 1)
    N_techs = 18

    # Data have been loaded with the correct shape, unit and magnitude:
    # 1. Shape
    assert inv_rail_pub.shape == (rows_per_tech, 5), inv_rail_pub
    assert inv.shape == (rows_per_tech * N_techs, 5)

    # 2. Units
    units = inv_rail_pub["unit"].unique()
    assert len(units) == 1, "Units for each (par, tec) must be unique"

    # Unit is parseable by pint
    pint_unit = registry(units[0])

    # Unit has the correct dimensionality
    assert pint_unit.dimensionality == {"[currency]": 1, "[vehicle]": -1}

    # 3. Magnitude for year e.g. 2020
    values = inv_rail_pub[inv_rail_pub["year_vtg"] == 2020]["value"]
    value = values.iloc[0]
    assert round(value, 3) == 3.233

    dims = {
        "technical_lifetime": {"[time]": 1},
        # Output units are in (passenger km) / energy, that's why mass and
        # time dimensions have to be checked.
        "output": {"[passenger]": 1, "[length]": -1, "[mass]": -1, "[time]": 2},
        "capacity_factor": {
            "[passenger]": 1,
            "[length]": 1,
            "[vehicle]": -1,
            "[time]": -1,
        },
        "fix_cost": {"[currency]": 1, "[vehicle]": -1, "[time]": -1},
    }
    # Check dimensionality of ikarus pars with items in dims:
    for par, dim in dims.items():
        units = data[par]["unit"].unique()
        assert len(units) == 1, "Units for each (par, tec) must be unique"
        # Unit is parseable by pint
        pint_unit = registry(units[0])
        # Unit has the correct dimensionality
        assert pint_unit.dimensionality == dim

    # Specific magnitudes of other values to check
    checks = [
        dict(par="capacity_factor", year_vtg=2010, value=0.000905),
        dict(par="technical_lifetime", year_vtg=2010, value=14.7),
        dict(par="capacity_factor", year_vtg=2050, value=0.000886),
        dict(par="technical_lifetime", year_vtg=2050, value=14.7),
    ]
    defaults = dict(node_loc=s_info.N[-1], technology="ICG_bus", time="year")

    for check in checks:
        # Create expected data
        par_name = check.pop("par")
        check["year_act"] = check["year_vtg"]
        exp = make_df(par_name, **defaults, **check)
        assert len(exp) == 1, "Single row for expected value"

        # Use merge() to find data with matching column values
        columns = sorted(set(exp.columns) - {"value", "unit"})
        result = exp.merge(data[par_name], on=columns, how="inner")

        # Single row matches
        assert len(result) == 1, result

        # Values match
        assert_series_equal(
            result["value_x"],
            result["value_y"],
            check_exact=False,
            check_names=False,
            atol=1e-4,
        )


@pytest.mark.parametrize("source, rows", (("1", 15839), ("2", 17286), ("3", 5722)))
def test_get_emissions_data(transport_context_f, source, rows):
    ctx = transport_context_f
    ctx["transport config"]["data source"]["emissions"] = source

    data = get_emissions_data(ctx)
    assert {"emission_factor"} == set(data.keys())
    assert rows == len(data["emission_factor"])


@pytest.mark.parametrize(
    "source, regions, years",
    [
        (None, "R11", "A"),
        ("US-TIMES MA3T", "R11", "A"),
        # Not implemented
        param("US-TIMES MA3T", "R11", "B", marks=testing.NIE),
        param("US-TIMES MA3T", "R14", "A", marks=testing.NIE),
        param("US-TIMES MA3T", "ISR", "A", marks=testing.NIE),
    ],
)
def test_get_ldv_data(transport_context_f, source, regions, years):
    ctx = transport_context_f

    # Info about the corresponding RES
    ctx.regions = regions
    ctx.years = years
    info = bare.get_spec(ctx)["add"]

    ctx["transport build info"] = info

    # Method runs without error
    ctx["transport config"]["data source"]["LDV"] = source
    data = get_ldv_data(ctx)

    # Output data is returned
    assert "output" in data

    for bound in ("lo", "up"):
        # Constraint data are returned
        df = data.pop(f"growth_activity_{bound}")

        # Data covers all periods except the first
        assert info.Y[1:] == sorted(df["year_act"].unique())

    # Data have the correct size
    for par_name, df in data.items():
        # Data covers all the years in the scenario, plus 2010
        assert [2010] + info.Y == sorted(df["year_vtg"].unique())

        # Total length of data: # of regions × (11 technology × # of periods; plus 1
        # technology (historical ICE) for only 2010)
        assert len(info.N[1:]) * ((11 * (len(info.Y) + 1)) + 1) == len(df)


@pytest.mark.parametrize(
    "regions", ["R11", param("R14", marks=testing.NIE), param("ISR", marks=testing.NIE)]
)
@pytest.mark.parametrize("pop_scen", ["GEA mix"])
def test_groups(transport_context_f, regions, pop_scen):
    ctx = transport_context_f
    ctx.regions = regions
    ctx["transport population scenario"] = pop_scen
    ctx["transport build info"] = bare.get_spec(ctx)["add"]

    result = get_consumer_groups(ctx)

    # Data have the correct size
    exp = dict(n=11, y=15, cg=27)

    # NB as of genno 1.3.0, can't use .sizes on AttrSeries:
    # assert result.sizes == exp
    obs = {dim: len(result.coords[dim]) for dim in exp.keys()}
    assert exp == obs, result.coords

    # Data sum to 1 across the consumer_group dimension, i.e. constitute a discrete
    # distribution
    assert (result.sum("cg") - 1.0 < 1e-08).all()


@pytest.mark.parametrize(
    "regions", ["R11", param("R14", marks=testing.NIE), param("ISR", marks=testing.NIE)]
)
@pytest.mark.parametrize("pop_scen", ["GEA mix", "GEA supply", "GEA eff"])
def test_urban_rural_shares(transport_context_f, regions, pop_scen):
    ctx = transport_context_f
    ctx.regions = regions
    ctx["transport"] = {"data source": {"population": pop_scen}}
    ctx["transport build info"] = bare.get_spec(ctx)["add"]

    # Shares can be retrieved
    get_urban_rural_shares(ctx)
