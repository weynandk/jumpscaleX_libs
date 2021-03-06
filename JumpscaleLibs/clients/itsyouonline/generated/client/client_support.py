"""
support methods for python clients
"""

import json
import collections
from datetime import datetime
from uuid import UUID
from enum import Enum
from dateutil import parser
from Jumpscale import j

# python2/3 compatible basestring, for use in to_dict
try:
    basestring
except NameError:
    basestring = str


def timestamp_from_datetime(datetime):
    """
        Convert from datetime format to timestamp format
        Input: Time in datetime format
        Output: Time in timestamp format
    """
    return datetime.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def timestamp_to_datetime(timestamp):
    """
        Convert from timestamp format to datetime format
        Input: Time in timestamp format
        Output: Time in datetime format
    """
    return parser.parse(timestamp).replace(tzinfo=None)


def has_properties(cls, property, child_properties):
    for child_prop in child_properties:
        if getattr(property, child_prop, None) is None:
            return False

    return True


def list_factory(val, member_type):
    if not isinstance(val, list):
        raise j.exceptions.Value("list_factory: value must be a list")
    return [val_factory(v, member_type) for v in val]


def dict_factory(val, objmap):
    # objmap is a dict outlining the structure of this value
    # its format is {'attrname': {'datatype': [type], 'required': bool}}
    objdict = {}
    for attrname, attrdict in objmap.items():
        value = val.get(attrname)
        if value is not None:
            for dt in attrdict["datatype"]:
                try:
                    if isinstance(dt, dict):
                        objdict[attrname] = dict_factory(value, attrdict)
                    else:
                        objdict[attrname] = val_factory(value, [dt])
                except Exception:
                    pass
            if objdict.get(attrname) is None:
                raise j.exceptions.Value(
                    "dict_factory: {attr}: unable to instantiate with any supplied type".format(attr=attrname)
                )
        elif attrdict.get("required"):
            raise j.exceptions.Value("dict_factory: {attr} is required".format(attr=attrname))

    return objdict


def val_factory(val, datatypes):
    """
    return an instance of `val` that is of type `datatype`.
    keep track of exceptions so we can produce meaningful error messages.
    """
    exceptions = []
    for dt in datatypes:
        try:
            if isinstance(val, dt):
                return val
            return type_handler_object(val, dt)
        except Exception as e:
            exceptions.append(str(e))
    # if we get here, we never found a valid value. raise an error
    raise j.exceptions.Value(
        "val_factory: Unable to instantiate {val} from types {types}. Exceptions: {excs}".format(
            val=val, types=datatypes, excs=exceptions
        )
    )


def set_property(
    name, data, data_types, has_child_properties, required_child_properties, is_list, required, class_name
):
    """
    Set a class property
    :param name: property name to set
    :param data: class data
    :param data_types: the data types this property supports
    :param has_child_properties: boolean indicating if the property has child properties
    :param required_child_properties: a list of required child properties
    :param is_list: boolean indicating if this property is a list
    :param required: boolean indicating if this property is required or not
    :param class_name: name of the class this property belongs to
    :return:
    """
    create_error = "{cls}: unable to create {prop} from value: {val}: {err}"
    required_error = "{cls}: missing required property {prop}"
    factory_value = None
    val = data.get(name)
    if val is not None:
        try:
            if is_list:
                factory_value = list_factory(val, data_types)
            elif has_child_properties:
                factory_value = dict_factory(val, data_types)
            else:
                factory_value = val_factory(val, data_types)
        except ValueError as err:
            raise j.exceptions.Value(create_error.format(cls=class_name, prop=name, val=val, err=err))
        else:
            if required_child_properties:
                for child in required_child_properties:
                    if not factory_value.get(child):
                        child_prop_name = "{parent}.{child}".format(parent=name, child=child)
                        raise j.exceptions.Value(required_error.format(cls=class_name, prop=child_prop_name))
    elif required:
        raise j.exceptions.Value(required_error.format(cls=class_name, prop=name))

    return factory_value


def to_json(cls, indent=0):
    """
    serialize to JSON
    :rtype: str
    """
    # for consistency, use as_dict then go to json from there
    return json.dumps(cls.as_dict(), indent=indent)


def to_dict(cls, convert_datetime=True):
    """
    return a dict representation of the Event and its sub-objects
    `convert_datetime` controls whether datetime objects are converted to strings or not
    :rtype: dict
    """

    def todict(obj):
        """
        recurse the objects and represent as a dict
        use the registered handlers if possible
        """
        data = {}
        if isinstance(obj, dict):
            for (key, val) in obj.items():
                data[key] = todict(val)
            return data
        if not convert_datetime and isinstance(obj, datetime):
            return obj
        elif type_handler_value(obj):
            return type_handler_value(obj)
        elif isinstance(obj, collections.Sequence) and not isinstance(obj, basestring):
            return [todict(v) for v in obj]
        elif hasattr(obj, "__dict__"):
            for key, value in obj.__dict__.items():
                if not callable(value) and not key.startswith("_"):
                    data[key] = todict(value)
            return data
        else:
            return obj

    return todict(cls)


class DatetimeHandler:
    """
    output datetime objects as iso-8601 compliant strings
    """

    def __init__(self):
        pass

    @classmethod
    def flatten(cls, obj):
        """flatten"""
        return timestamp_from_datetime(obj)

    @classmethod
    def restore(cls, data):
        """restore"""
        return timestamp_to_datetime(data)


class UUIDHandler:
    """
    output UUID objects as a string
    """

    def __init__(self):
        pass

    @classmethod
    def flatten(cls, obj):
        """flatten"""
        return str(obj)

    @classmethod
    def restore(cls, data):
        """restore"""
        return UUID(data)


class EnumHandler:
    """
    output Enum objects as their value
    """

    def __init__(self):
        pass

    @classmethod
    def flatten(cls, obj):
        """flatten"""
        return obj.value

    @classmethod
    def restore(cls, data):
        """
        cannot restore here because we don't know what type of enum it is
        """
        raise j.exceptions.NotImplemented


handlers = {datetime: DatetimeHandler, Enum: EnumHandler, UUID: UUIDHandler}


def handler_for(obj):
    """return the handler for the object type"""
    for handler_type in handlers:
        if isinstance(obj, handler_type):
            return handlers[handler_type]

    try:
        for handler_type in handlers:
            if issubclass(obj, handler_type):
                return handlers[handler_type]
    except TypeError:
        # if obj isn't a class, issubclass will raise a TypeError
        pass


def type_handler_value(obj):
    """
    return the serialized (flattened) value from the registered handler for the type
    """
    handler = handler_for(obj)
    if handler:
        return handler().flatten(obj)


def type_handler_object(val, objtype):
    """
    return the deserialized (restored) value from the registered handler for the type
    """
    handler = handlers.get(objtype)
    if handler:
        return handler().restore(val)
    else:
        return objtype(val)
