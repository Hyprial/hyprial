"""Pure settings contract declarations; importing this module binds no runtime handler."""

STRING = {'type': 'string'}

NULL_STRING = {'type': ['string', 'null']}

def record(properties, required=None):
    return {'type': 'object', 'properties': properties,
            'required': list(properties) if required is None else required, 'additionalProperties': False}

USER = record({'userKey': STRING, 'kind': {'type': 'string', 'enum': ['member', 'guest']},
    'owner': NULL_STRING, 'nickname': NULL_STRING, 'realName': NULL_STRING,
    'displayName': NULL_STRING, 'createdAtMs': {'type': 'integer'}, 'updatedAtMs': {'type': 'integer'}})

USERS_RESULT = record({'users': {'type': 'array', 'items': USER}})
