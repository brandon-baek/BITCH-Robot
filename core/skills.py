"""On-demand routines composed from the robot's available capabilities."""
import json
import math
import re

from stage import MAX_STEPS, OPERATIONS

from . import llm, memory

CREATE = re.compile(
    r"\b(?:make(?: up)?|invent|create|build|learn|choreograph|come up with|put together|teach yourself)"
    r"\b.{0,52}\b(?:routine|skill|sequence|dance|trick|choreography)\b", re.I)
NAME = re.compile(r'\b(?:called|named)\s+["“]?([a-zA-Z][a-zA-Z0-9 -]{0,36}?)'
                  r'(?=["”:,;.!?]| (?:that|where|with|which)\b|$)', re.I)
RUN = re.compile(r'^(?:(?:can|could|would) you |please )?(?:do|run|perform|play|show me)'
                 r'\s+(?:the )?["“]?(.+?)["”]?[.!?]*$', re.I)
TEACH = re.compile(r'^when I say ["“]?([a-z][a-z0-9 -]{0,36}?)["”]?\s*[,;:]\s*(\S.+)$', re.I)
CONDITIONAL = re.compile(r'\b(?:if|when|while|unless|otherwise|only|depending)\b', re.I)
WHEN = ('always', 'person_visible', 'room_empty')
LOOK = ('person', 'point', 'finger')
CAPABILITIES = {**OPERATIONS, 'look': LOOK, 'say': 'literal spoken line'}
SCHEMA = {
    'type': 'object',
    'properties': {
        'steps': {
            'type': 'array',
            'maxItems': MAX_STEPS,
            'items': {
                'type': 'object',
                'properties': {
                    'op': {'type': 'string', 'enum': list(CAPABILITIES)},
                    'style': {'type': 'string', 'maxLength': 100},
                    'seconds': {'type': 'number', 'minimum': .3, 'maximum': 4},
                    'when': {'type': 'string', 'enum': list(WHEN)},
                },
                'required': ['op', 'style', 'seconds', 'when'],
                'additionalProperties': False,
            },
        },
        'missing': {'type': 'string', 'maxLength': 120},
    },
    'required': ['steps', 'missing'],
    'additionalProperties': False,
}
DIRECTION = f"""Design a short desk robot routine. Return only the requested JSON object.
Supported op/style combinations: {json.dumps(CAPABILITIES)}.
Use look/person for a single visible person, look/point to look where the person is pointing, and look/finger to briefly follow their fingertip. For say, style is the exact spoken line, up to 100 characters. Split a new behavior into steps in the requested order. The robot has no gripper and cannot move objects.
Use 1–12 steps, .3 to 4 seconds per step, up to 30 seconds total. Suggest 1–2 seconds per gesture.
Each step runs when its time comes. Set when to always unless the user requests a condition at startup:
person_visible means the camera has a face in view; room_empty means the camera currently detects no faces.
Use only these observations. If a required capability is missing, return empty steps and briefly describe what is missing.
Keep missing empty when all requested behavior is expressible. Don't claim to have performed any action."""


def validate_plan(steps):
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError('routine needs 1–12 steps')
    total = 0.
    for step in steps:
        if not isinstance(step, dict) or step.keys() != {'op', 'style', 'seconds', 'when'} or step['when'] not in WHEN:
            raise ValueError('invalid routine step or condition')
        op, style, seconds = step['op'], step['style'], step['seconds']
        if (not isinstance(op, str) or op not in CAPABILITIES or not isinstance(style, str) or not style.strip()
                or type(seconds) not in (float, int) or not math.isfinite(seconds) or not .3 <= seconds <= 4):
            raise ValueError('invalid routine action or duration')
        if op == 'say':
            if len(style) > 100 or not style.isprintable():
                raise ValueError('speech needs one short line')
        elif style not in CAPABILITIES[op]:
            raise ValueError('unknown routine action')
        total += seconds
    if total > 30 or all(step['op'] == 'pause' for step in steps):
        raise ValueError('routine needs an action and may run for at most 30 seconds')
    return steps


def draft(request):
    messages = [{'role': 'system', 'content': DIRECTION}, {'role': 'user', 'content': request}]
    plan = json.loads(llm.chat(messages, max_tokens=620, schema=SCHEMA))
    if not isinstance(plan, dict) or plan.keys() != {'steps', 'missing'} or not isinstance(plan['missing'], str):
        raise ValueError('invalid routine')
    missing = ' '.join(plan['missing'].split())
    if len(missing) > 120:
        missing = missing[:119].rsplit(' ', 1)[0].rstrip(',.!?') + '.'
    if missing:
        return None, missing
    steps = validate_plan(plan['steps'])
    if not CONDITIONAL.search(request):
        steps = [{**step, 'when': 'always'} for step in steps]
    return steps, ''


def saved(name):
    return memory.kv_get('robot-skill:' + name.casefold())


def names():
    return [key.split(':', 1)[1] for key in memory.kv_keys('robot-skill:')]


def save(name, plan):
    memory.kv_set('robot-skill:' + name.casefold(), validate_plan(plan))


def actions(plan, visible):
    return [{key: value for key, value in step.items() if key != 'when'} for step in validate_plan(plan)
            if step['when'] == 'always' or visible is not None and
            step['when'] == ('person_visible' if visible else 'room_empty')]
