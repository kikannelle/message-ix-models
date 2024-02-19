"""Data preparation for the MESSAGEix-GLOBIOM base model."""
from functools import partial
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    import message_ix


SCALE_1_HEADER = """Ratio of MESSAGEix-Transport output to IEA EWEB data.

- `t` (technology) codes correspond to IEA `FLOW` codes or equivalent aggregates
  across groups of MESSAGEix-Transport technologies.
- `c` (commodity) codes correspond to MESSAGEix-GLOBIOM commodities or equivalent
  aggregates across groups of IEA `PRODUCT` codes.
"""

SCALE_2_HEADER = """Ratio of scaled MESSAGEix-Transport output to IEA EWEB data.

The numerator used to compute this scaling factor is the one corrected by the values in
scale-1.csv.
"""


def prepare_reporter(rep: "message_ix.Reporter") -> str:
    """Add tasks that produce data to parametrize transport in MESSAGEix-GLOBIOM.

    Returns a key, "base model data". Retrieving the key results in the creation of 5
    files in the reporting output directory for the :class:`.Scenario` being reported
    (see :func:`.make_output_path`):

    1. :file:`demand.csv`: This contains MESSAGEix-Transport model solution data
       transformed into ``demand`` parameter data for a base MESSAGEix-GLOBIOM model—
       that is, one without MESSAGEix-Transport. :file:`input-base.csv` is used.
    2. :file:`bound_activity_lo.csv`: Same data transformed into ``bound_activity_lo``
       parameter data for the transport technologies ("coal_trp", etc.) appearing in the
       base model.

       .. todo:: Drop ``bound_activity_lo`` values that are equal to zero.
    3. :file:`bound_activity_up.csv`: Same values as (2), multiplied by 1.005.

    Two files are for diagnosis:

    4. :file:`scale-1.csv`: First stage scaling factor used to bring MESSAGEix-Transport
       (c, t) totals in correspondence with IEA World Energy Balance (WEB) values.
    5. :file:`scale-2.csv`: Second stage scaling factor used to bring overall totals.
    """
    from genno import Key, KeySeq, Quantity, quote

    # Final key
    targets = []

    e_iea = Key("energy:n-y-product-flow:iea")
    e_fnp = KeySeq(e_iea.drop("y"))
    e_cnlt = Key("energy:c-nl-t:iea+0")
    k = KeySeq("in:nl-t-ya-c-l-h:transport+units")

    # Transform IEA EWEB data for comparison
    rep.add(e_fnp[0], "select", e_iea, indexers=dict(y=2020), drop=True)
    rep.add(e_fnp[1], "aggregate", e_fnp[0], "groups::iea to transport", keep=False)
    rep.add(
        e_cnlt,
        "rename_dims",
        e_fnp[1],
        quote(dict(flow="t", n="nl", product="c")),
        sums=True,
    )

    # Transport outputs for comparison
    rep.add(k[0], "select", k.base, indexers=dict(ya=2020), drop=True)
    rep.add(k[1], "aggregate", k[0], "groups::transport to iea", keep=False)

    # Scaling factor 1: ratio of MESSAGEix-Transport outputs to IEA data
    tmp = rep.add("scale 1", "div", k[1], e_cnlt)
    s1 = KeySeq(tmp)
    rep.add(s1[1], "convert_units", s1.base, units="1 / a")
    rep.add(s1[2], "mul", s1[1], Quantity(1.0, units="a"))

    def _to_csv(base: "Key", name: str, hc):
        """Helper to add computations to output data to CSV."""
        # Some strings
        csv, path, fn = f"{name} csv", f"{name} path", f"{name.replace(' ', '-')}.csv"
        # Output path for this parameter
        rep.add(path, "make_output_path", "config", "scenario", fn)
        # Write to file
        rep.add(csv, "write_report", base, path, hc)
        targets.append(csv)

    _to_csv(s1[2], s1.name, dict(header_comment=SCALE_1_HEADER))

    # Clip values to 1.0; this avoids x / 0 = inf
    rep.add(s1[3], "clip", s1[2], lower=1.0)
    # Restore original "t" labels to scale-1
    rep.add(s1[4], "select", s1[3], "indexers::iea to transport")
    rep.add(s1[5], "rename_dims", s1[4], quote(dict(t_new="t")))
    # Correct MESSAGEix-Transport outputs for the MESSAGEix-base model using the high-
    # resolution scaling factor
    rep.add(k["s1"], "div", k.base, s1[5])

    # Scaling factor 2: ratio of total of scaled data to IEA total
    rep.add(
        k[2] / "ya", "select", k["s1"], indexers=dict(ya=2020), drop=True, sums=True
    )
    rep.add(
        "energy:nl:iea+transport",
        "select",
        e_cnlt / "c",
        indexers=dict(t="transport"),
        drop=True,
    )
    tmp = rep.add("scale 2", "div", k[2] / ("c", "t", "ya"), "energy:nl:iea+transport")
    s2 = KeySeq(tmp)

    rep.add(s2[1], "convert_units", s2.base, units="1 / a")
    rep.add(s2[2], "mul", s2[1], Quantity(1.0, units="a"))

    _to_csv(s2[2], s2.name, dict(header_comment=SCALE_2_HEADER))

    # Correct MESSAGEix-Transport outputs using the low-resolution scaling factor
    rep.add(k["s2"], "div", k["s1"], s2[2])

    # Convert "final" energy inputs to transport to "useful energy" outputs, using
    # efficiency data from input-base.csv (in turn, from the base model). This data
    # will be used for `demand`.
    # - Sum across the "t" dimension of `k` to avoid conflict with "t" labels introduced
    #   by the data from file.
    ue = rep.add("ue", "div", k["s2"] / "t", "input:t-c-h:base")
    assert isinstance(ue, Key)

    # Ensure units: in::transport+units [=] GWa/a and input::base [=] GWa; their ratio
    # gives units 1/a. The base model expects "GWa" for all 3 parameters.
    rep.add(ue + "1", "mul", ue, Quantity(1.0, units="GWa * a"))

    # Select only ya=2020 data for use in `bound_activity_*`
    b_a_l = rep.add(Key("b_a_l", ue.dims), "select", ue + "1", quote(dict(ya=[2020])))

    # `bound_activity_up` values are 1.005 * `bound_activity_lo` values
    b_a_u = rep.add("b_a_u", "mul", b_a_l, Quantity(1.005))

    # Keyword arguments for as_message_df()
    args_demand = dict(
        dims=dict(node="nl", year="ya", time="h"),
        common=dict(commodity="transport", level="useful"),
    )
    args_bound_activity = dict(
        dims=dict(node_loc="nl", technology="t", year_act="ya", time="h"),
        common=dict(mode="M1"),
    )

    # Add similar steps for each parameter
    for name, base_key, args in (
        ("demand", (ue + "1").drop("c", "t"), args_demand),
        ("bound_activity_lo", b_a_l, args_bound_activity),
        ("bound_activity_up", b_a_u, args_bound_activity),
    ):
        # More identifiers
        s = f"base model transport {name}"
        key, header = Key(f"{s}::ixmp"), f"{s} header"

        # Convert to MESSAGE data structure
        rep.add(key, "as_message_df", base_key, name=name, wrap=False, **args)

        # Sort values
        # TODO Move upstream as a feature of as_message_df()
        dims = list(args["dims"])
        rep.add(key + "1", partial(pd.DataFrame.sort_values, by=dims), key)

        # Header for this file
        rep.add(header, "base_model_data_header", "scenario", name=name)

        _to_csv(key + "1", name, header)

    # Key to trigger all the above
    result = "base model data"
    rep.add(result, targets)

    return result
