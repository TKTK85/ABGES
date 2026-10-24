"""Data-only checkpoints. Never import a type named by an input document."""
import datetime
import importlib
import math


_RESULT_TYPES = {
    'abges_phenotype': {'PhenotypeQCResult', 'MultiTraitQCResult'},
    'abges_pedigree': {'PedigreeQCResult'},
    'abges_integration': {'IntegrationResult'},
    'abges_traits': {'DerivedTraitResult'},
    'abges_validate': {'ValidationResult'},
    'abges_core': {'StatisticsSnapshot'},
}


def encode(value):
    import numpy as np
    import pandas as pd
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {'type': 'float', 'value': str(value)}
    if value is pd.NA or value is pd.NaT:
        return {'type': 'missing'}
    if isinstance(value, np.generic):
        return encode(value.item())
    if isinstance(value, (datetime.datetime, datetime.date, pd.Timestamp)):
        return {'type': 'datetime', 'value': value.isoformat()}
    if isinstance(value, dict):
        return {'type': 'dict', 'items': [[encode(k), encode(v)] for k, v in value.items()]}
    if isinstance(value, (list, tuple, set)):
        return {'type': type(value).__name__, 'items': [encode(v) for v in value]}
    if isinstance(value, pd.DataFrame):
        return {'type': 'frame', 'columns': encode(list(value.columns)),
                'series': [encode(value.iloc[:, i]) for i in range(len(value.columns))]}
    if isinstance(value, pd.Series):
        result = {'type': 'series', 'values': encode(value.tolist()), 'index': encode(value.index.tolist()),
                  'name': encode(value.name), 'dtype': str(value.dtype)}
        if isinstance(value.dtype, pd.CategoricalDtype):
            result.update(categories=encode(value.cat.categories.tolist()), ordered=value.cat.ordered)
        return result
    if isinstance(value, np.ndarray):
        return {'type': 'array', 'dtype': str(value.dtype), 'values': encode(value.tolist())}
    module, name = type(value).__module__, type(value).__name__
    if name in _RESULT_TYPES.get(module, set()):
        return {'type': 'result', 'module': module, 'name': name, 'attrs': encode(vars(value))}
    raise ValueError('unsupported checkpoint data type: ' + module + '.' + name)


def decode(value):
    import numpy as np
    import pandas as pd
    if not isinstance(value, dict):
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        raise ValueError('invalid checkpoint value')
    kind = value.get('type')
    if kind == 'float':
        if value['value'] not in {'nan', 'inf', '-inf'}:
            raise ValueError('invalid float')
        return float(value['value'])
    if kind == 'missing': return pd.NA
    if kind == 'datetime': return pd.Timestamp(value['value'])
    if kind == 'dict': return {decode(k): decode(v) for k, v in value['items']}
    if kind in {'list', 'tuple', 'set'}:
        return {'list': list, 'tuple': tuple, 'set': set}[kind](decode(v) for v in value['items'])
    if kind == 'series':
        dtype = value['dtype']
        if dtype == 'category':
            dtype = pd.CategoricalDtype(decode(value['categories']), ordered=value['ordered'])
        return pd.Series(decode(value['values']), index=decode(value['index']), name=decode(value['name']), dtype=dtype)
    if kind == 'frame':
        series = [decode(v) for v in value['series']]
        frame = pd.concat(series, axis=1) if series else pd.DataFrame()
        frame.columns = decode(value['columns'])
        return frame
    if kind == 'array': return np.array(decode(value['values']), dtype=value['dtype'])
    if kind == 'result' and value.get('name') in _RESULT_TYPES.get(value.get('module'), set()):
        cls = getattr(importlib.import_module(value['module']), value['name'])
        obj = object.__new__(cls)
        obj.__dict__.update(decode(value['attrs']))
        return obj
    raise ValueError('unsupported checkpoint tag')
