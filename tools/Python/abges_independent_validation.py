# -*- coding: utf-8 -*-
"""Independent holdout validation for frozen genetic predictions.

Inputs must be from a validation cohort whose phenotypes were excluded from
the prediction fit. The validation phenotype must already be adjusted to the
target's model scale (for example, fixed-effect corrected); raw phenotype is
not silently treated as a prediction target.
"""
import argparse
import math
from pathlib import Path

import pandas as pd


def _read(path):
    path = Path(path)
    sep = "\t" if path.suffix.lower() in {".tsv", ".txt"} else ","
    return pd.read_csv(path, sep=sep, dtype={"ID": str})


def _normalize_id(value):
    text = str(value).strip()
    try:
        number = float(text)
        if math.isfinite(number) and number.is_integer():
            return str(int(number))
    except ValueError:
        pass
    return text


def evaluate(predictions, validation, training_ids, adjusted_column="adjusted_phenotype",
             min_n=30):
    """Return holdout prediction metrics, refusing train/validation ID overlap."""
    pred = _read(predictions)
    observed = _read(validation)
    train = _read(training_ids)
    id_col = "ID" if "ID" in train.columns else train.columns[0]
    train_ids = {_normalize_id(value) for value in train[id_col].dropna()}
    return evaluate_frames(pred, observed, train_ids, adjusted_column, min_n)


def evaluate_frames(pred, observed, train_ids, adjusted_column="adjusted_phenotype",
                    min_n=30):
    """DataFrame/set variant used by the ABGES runtime to keep validation auditable."""
    pred = pred.copy()
    observed = observed.copy()
    for name, frame, required in (
        ("predictions", pred, {"ID", "EBV"}),
        ("validation", observed, {"ID", adjusted_column}),
    ):
        missing = required - set(frame.columns)
        if missing:
            raise ValueError("%s missing required columns: %s" % (name, sorted(missing)))
    if pred["ID"].duplicated().any() or observed["ID"].duplicated().any():
        raise ValueError("ID must be unique in prediction and validation files")
    train_ids = {_normalize_id(value) for value in train_ids}
    observed_ids = {_normalize_id(value) for value in observed["ID"].dropna()}
    overlap = train_ids & observed_ids
    if overlap:
        raise ValueError("validation IDs overlap training phenotype IDs (%d); holdout is not independent"
                         % len(overlap))
    pred["ID"] = pred["ID"].map(_normalize_id)
    observed["ID"] = observed["ID"].map(_normalize_id)
    joined = observed[["ID", adjusted_column]].merge(pred[["ID", "EBV"]], on="ID", how="inner")
    joined[adjusted_column] = pd.to_numeric(joined[adjusted_column], errors="coerce")
    joined["EBV"] = pd.to_numeric(joined["EBV"], errors="coerce")
    joined = joined.dropna(subset=[adjusted_column, "EBV"])
    n = int(len(joined))
    if n < 2:
        raise ValueError("fewer than two independent validation predictions matched")
    x = joined["EBV"].to_numpy(dtype=float)
    y = joined[adjusted_column].to_numpy(dtype=float)
    residual = y - x
    correlation = float(pd.Series(x).corr(pd.Series(y)))
    rank_correlation = float(pd.Series(x).corr(pd.Series(y), method="spearman"))
    slope, intercept = (float(v) for v in __import__("numpy").polyfit(x, y, 1))
    return {
        "status": "evaluated" if n >= int(min_n) else "insufficient_sample",
        "n": n, "minimum_n": int(min_n), "training_validation_overlap": 0,
        "pearson_predictive_ability": correlation,
        "spearman_rank_correlation": rank_correlation,
        "calibration_slope": slope, "calibration_intercept": intercept,
        "mean_bias_adjusted_phenotype_minus_ebv": float(residual.mean()),
        "rmse": math.sqrt(float((residual ** 2).mean())),
        "interpretation": (
            "独立验证群表现指标；相关性是预测能力，不等同于遗传评估准确度。"
            "如需准确度估计，须另有适用且明确的验证群遗传力/验证设计。"
        ),
    }


def write_report(path, result):
    lines = ["# 独立预测验证报告", "", "| 指标 | 结果 |", "|---|---:|"]
    for key in ("status", "n", "minimum_n", "training_validation_overlap",
                "pearson_predictive_ability", "spearman_rank_correlation",
                "calibration_slope", "calibration_intercept",
                "mean_bias_adjusted_phenotype_minus_ebv", "rmse"):
        lines.append("| %s | %s |" % (key, result.get(key)))
    lines.extend(["", result["interpretation"], ""])
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", required=True, help="CSV/TSV with ID,EBV")
    parser.add_argument("--validation", required=True,
                        help="independent validation CSV/TSV with ID,adjusted_phenotype")
    parser.add_argument("--training-ids", required=True,
                        help="training phenotype IDs, one ID column")
    parser.add_argument("--adjusted-column", default="adjusted_phenotype")
    parser.add_argument("--min-n", type=int, default=30)
    parser.add_argument("--report", required=True, help="Markdown report destination")
    args = parser.parse_args(argv)
    result = evaluate(args.predictions, args.validation, args.training_ids,
                      args.adjusted_column, args.min_n)
    write_report(args.report, result)
    print("%s: n=%d report=%s" % (result["status"], result["n"], args.report))
    return 0 if result["status"] == "evaluated" else 2


if __name__ == "__main__":
    raise SystemExit(main())
