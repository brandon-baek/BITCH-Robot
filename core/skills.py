"""On-demand routines composed from the robot's available capabilities."""
import json
import re

from stage import MAX_STEPS, OPERATIONS, validate_steps

from . import llm, memory

CREATE = re.compile(
    r"\b(?:make(?: up)?|invent|create|build|choreograph|come up with|put together|teach yourself)"
    r"\b.{0,52}\b(?:routine|skill|sequence|dance|trick|choreography)\b", re.I)
NAME = re.compile(r'\b(?:called|named)\s+["“]?([a-zA-Z][a-zA-Z0-9 -]{0,36}?)'
                  r'(?=["”:,;.!?]| (?:that|where|with|which)\b|$)', re.I)
RUN = re.compile(r'^(?:(?:can|could|would) you |please )?(?:do|run|perform|play|show me)'
                 r'\s+(?:the )?["“]?(.+?)["”]?[.!?]*$', re.I)
WHEN = ('always', 'person_visible', 'room_empty')
SCHEMA = {
    'type': 'object',
    'properties': {
        'steps': {
            'type': 'array',
            'maxItems': MAX_STEPS,
            'items': {
                'type': 'object',
                'properties': {
                    'op': {'type': 'string', 'enum': list(OPERATIONS)},
                    'style': {'type': 'string', 'enum': [style for group in OPERATIONS.values() for style in group]},
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
Supported op/style combinations: {json.dumps(OPERATIONS)}.
The robot can turn and tilt its head, nod its neck, lean and chirp. Its external camera can count visible faces. It has no gripper and cannot pick things up.
Use 1–12 steps, .3 to 4 seconds per step, up to 30 seconds total. Suggest 1–2 seconds per gesture.
Each step runs when its time comes. Set when to always unless the user requests a condition at startup:
person_visible means the camera has a face in view; room_empty means the camera currently detects no faces.
Use only these observations. If a required capability is missing, return empty steps and briefly describe what is missing.
Keep missing empty when all requested behavior is expressible. Don't claim to have performed any action."""


def validate_plan(steps):
    if not isinstance(steps, list) or any(
            not isinstance(step, dict) or step.get('when') not in WHEN for step in steps):
        raise ValueError('invalid routine condition')
    validate_steps([{key: value for key, value in step.items() if key != 'when'} for step in steps])
    return steps


def draft(request):
    messages = [{'role': 'system', 'content': DIRECTION}, {'role': 'user', 'content': request}]
    plan = json.loads(llm.chat(messages, max_tokens=620, schema=SCHEMA))
    if not isinstance(plan, dict) or plan.keys() != {'steps', 'missing'} or not isinstance(plan['missing'], str):
        raise ValueError('invalid routine')
    missing = ' '.join(plan['missing'].split())
    if len(missing) > 120:
        missing = missing[:119].rsplit(' ', 1)[0].rstrip(',.!?') + '.'
    return (None, missing) if missing else (validate_plan(plan['steps']), '')


def saved(name):
    return memory.kv_get('robot-skill:' + name.casefold())


def save(name, plan):
    memory.kv_set('robot-skill:' + name.casefold(), validate_plan(plan))


def actions(plan, visible):
    return [{key: value for key, value in step.items() if key != 'when'} for step in validate_plan(plan)
            if step['when'] == 'always' or visible is not None and
            step['when'] == ('person_visible' if visible else 'room_empty')]
