"""Validate task shape before filesystem effects; fingerprint physical inputs."""
from pathlib import Path
from .storage import identifier, sha256
import yaml


def _load_task_contract():
    schema_path = Path(__file__).resolve().parents[1] / 'schemas' / 'task-schema.yaml'
    with schema_path.open('r', encoding='utf-8') as stream:
        return yaml.safe_load(stream) or {}


def _validate_declared_task_contract(task):
    """Validate task keys and primitive shapes against task-schema.yaml.

    The YAML file is the runtime contract for allowed/required task fields;
    domain and filesystem checks remain explicit below.
    """
    contract = _load_task_contract()
    definition = contract.get('task') or {}
    required = {item['name']: item for item in definition.get('required_fields', [])}
    optional = {item['name']: item for item in definition.get('optional_fields', [])}
    allowed = set(required) | set(optional)
    missing = sorted(name for name in required if name not in task)
    unknown = sorted(set(task) - allowed)
    if missing or unknown:
        raise ValueError('task contract mismatch: missing=%s unknown=%s' % (missing, unknown))
    for name, item in {**required, **optional}.items():
        if name not in task:
            continue
        expected = item.get('type')
        value = task[name]
        if expected == 'string' and not isinstance(value, str):
            raise ValueError(name + ' must be a string')
        if expected == 'list[string]' and (not isinstance(value, list) or any(not isinstance(x, str) for x in value)):
            raise ValueError(name + ' must be a list of strings')
        if expected == 'list[enum]':
            if not isinstance(value, list) or any(x not in (item.get('allowed_values') or []) for x in value):
                raise ValueError(name + ' contains unsupported values')
        if expected == 'list[object]' and (not isinstance(value, list) or any(not isinstance(x, dict) for x in value)):
            raise ValueError(name + ' must be a list of mappings')
        if expected == 'object' and not isinstance(value, dict):
            raise ValueError(name + ' must be a mapping')
        if expected == 'enum' and value not in (item.get('allowed_values') or []):
            raise ValueError(name + ' has unsupported value')
        if expected == 'integer' and (not isinstance(value, int) or isinstance(value, bool)):
            raise ValueError(name + ' must be an integer')
        if expected == 'number' and (not isinstance(value, (int, float)) or isinstance(value, bool)):
            raise ValueError(name + ' must be numeric')
        if expected == 'boolean' and not isinstance(value, bool):
            raise ValueError(name + ' must be boolean')
        if expected == 'path' and (not isinstance(value, str) or not value):
            raise ValueError(name + ' must be a nonempty path')


def validate_task(task):
    if not isinstance(task, dict):
        raise ValueError('task must be a mapping')
    _validate_declared_task_contract(task)
    for key in ('task_id', 'task_name'):
        if key in task:
            identifier(task[key])
    if task.get('command', 'run') not in {'validate', 'plan', 'run', 'status'}:
        raise ValueError('unsupported command')
    model = task.get('model', 'auto')
    if model not in {'auto', 'pblup', 'gblup', 'ssgblup', 'ssblup'}:
        raise ValueError('unsupported model')
    if model == 'ssblup':
        task['model'] = 'ssgblup'
    for key in ('traits', 'fixed_effects', 'covariates'):
        if key in task and (not isinstance(task[key], list)
                           or any(not isinstance(v, str) or not v.strip() for v in task[key])):
            raise ValueError(key + ' must be a list of nonempty strings')
    if 'traits' in task and (not 1 <= len(task['traits']) <= 5 or len(set(task['traits'])) != len(task['traits'])):
        raise ValueError('traits must contain 1 to 5 unique entries')
    for key in ('inputs', 'engine_params', 'gateway_context', 'pre_standardized',
                'validation_adjusted_columns'):
        if key in task and not isinstance(task[key], dict):
            raise ValueError(key + ' must be a mapping')
    contract = _load_task_contract()
    input_field = next((item for item in contract.get('task', {}).get('required_fields', [])
                        if item.get('name') == 'inputs'), {})
    input_contract = {item['name']: item for item in input_field.get('fields', [])}
    for role, value in task.get('inputs', {}).items():
        if role not in input_contract:
            raise ValueError('unsupported input role: ' + str(role))
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError('input paths must be nonempty strings')
    if 'phenotype' not in task.get('inputs', {}):
        raise ValueError('inputs.phenotype is required')
    if 'output_dir' in task and (not isinstance(task['output_dir'], str) or not task['output_dir']):
        raise ValueError('output_dir must be a nonempty path')
    if 'multi_trait' in task and not isinstance(task['multi_trait'], bool):
        raise ValueError('multi_trait must be boolean')
    if 'recommend_decisions' in task:
        if not isinstance(task['recommend_decisions'], list) or any(not isinstance(v, dict) for v in task['recommend_decisions']):
            raise ValueError('recommend_decisions must be a list of mappings')
    reporting = task.get('genetic_reporting')
    if reporting is not None:
        if not isinstance(reporting, dict):
            raise ValueError('genetic_reporting must be a mapping')
        allowed = {'generation_semantics', 'reference_column', 'reference_value', 'minimum_n'}
        unknown = sorted(set(reporting) - allowed)
        if unknown:
            raise ValueError('genetic_reporting has unsupported fields: ' + ', '.join(unknown))
        semantics = reporting.get('generation_semantics', 'unspecified')
        if semantics not in {'genetic_generation', 'birth_cohort', 'management_period', 'unspecified'}:
            raise ValueError('genetic_reporting.generation_semantics is unsupported')
        if ('reference_column' in reporting) != ('reference_value' in reporting):
            raise ValueError('reference_column and reference_value must be provided together')
        if 'minimum_n' in reporting and (not isinstance(reporting['minimum_n'], int)
                                          or isinstance(reporting['minimum_n'], bool)
                                          or reporting['minimum_n'] < 1):
            raise ValueError('genetic_reporting.minimum_n must be a positive integer')
    scenario = task.get('selection_scenario')
    if scenario is not None:
        if not isinstance(scenario, dict) or set(scenario) - {'candidate_ids'}:
            raise ValueError('selection_scenario supports candidate_ids only')
        ids = scenario.get('candidate_ids', [])
        if not isinstance(ids, list) or any(not isinstance(x, str) or not x.strip() for x in ids):
            raise ValueError('selection_scenario.candidate_ids must be a list of IDs')
    if 'generation_interval' in task and (not isinstance(task['generation_interval'], (int, float))
                                           or isinstance(task['generation_interval'], bool)
                                           or task['generation_interval'] <= 0):
        raise ValueError('generation_interval must be positive when supplied')
    if 'validation_min_n' in task and (not isinstance(task['validation_min_n'], int)
                                       or isinstance(task['validation_min_n'], bool)
                                       or task['validation_min_n'] < 2):
        raise ValueError('validation_min_n must be an integer >= 2')
    columns = task.get('validation_adjusted_columns') or {}
    if any(not isinstance(key, str) or not isinstance(value, str) or not value.strip()
           for key, value in columns.items()):
        raise ValueError('validation_adjusted_columns must map trait codes to column names')
    if 'output_format' in task:
        profile_path = Path(__file__).resolve().parents[1] / 'configs' / 'delivery' / 'output-contract.yaml'
        with profile_path.open('r', encoding='utf-8') as stream:
            profile = yaml.safe_load(stream) or {}
        deliverables = profile.get('deliverables') or {}
        required_delivery = set(deliverables.get('required') or [])
        allowed_formats = required_delivery | set(deliverables.get('optional') or [])
        if (not isinstance(task['output_format'], list)
                or any(value not in allowed_formats for value in task['output_format'])):
            raise ValueError('output_format contains unsupported delivery formats')
        if not required_delivery.issubset(set(task['output_format'])):
            raise ValueError('output_format must include all required deliverables from output-contract.yaml')
    return task


def physical_inputs(inputs):
    result = {}
    for role, value in inputs.items():
        if not value:
            continue
        path = Path(value).resolve()
        if path.is_file():
            result[role] = path
        elif path.is_dir():
            for item in sorted(path.rglob('*')):
                if item.is_file():
                    result[role + '/' + item.relative_to(path).as_posix()] = item
        elif role == 'genotype':
            for suffix in ('.bed', '.bim', '.fam'):
                item = Path(str(path) + suffix)
                if not item.is_file():
                    raise ValueError('genotype input set is incomplete')
                result[role + suffix] = item
        else:
            raise ValueError('input does not exist: ' + role)
    return result


def input_manifest(inputs):
    return {key: {'path': str(path), 'sha256': sha256(path)}
            for key, path in physical_inputs(inputs).items()}
