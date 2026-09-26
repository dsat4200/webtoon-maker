"""Resolve imported physical lengths without changing their source values."""
from __future__ import annotations

from dataclasses import replace
import math

from .brushes import BrushDefinition, finite


DEFAULT_IMPORT_DPI = 300.0


def validate_import_dpi(dpi: float) -> float:
    try:
        dpi = float(dpi)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("Brush resolution must be a number between 1 and 9600 DPI.") from error
    if not math.isfinite(dpi) or not 1 <= dpi <= 9600:
        raise ValueError("Brush resolution must be between 1 and 9600 DPI.")
    return dpi


class BrushLengthConversion:
    """One import's explicit source-unit pairs and their pixel equivalents.

    CSP's observed codes are 0=pixels and 2=millimeters. Every distance has
    its own unit; a primary brush's unit never supplies a missing child unit.
    """
    def __init__(self, dpi: float = DEFAULT_IMPORT_DPI):
        self.dpi = validate_import_dpi(dpi)
        self.fields: dict[str, dict] = {}
        self.warnings: list[str] = []

    def length(self, target: str, field: str, value: float, unit=0, *,
               active: bool = True, minimum: float = 0., maximum: float = 10000.) -> float:
        value = finite(value)
        unit = finite(unit, -1) if unit is not None else 0
        pixels = value * self.dpi / 25.4 if unit == 2 else value
        result = max(minimum, min(maximum, pixels))
        if active and unit not in (0, 2):
            self.warnings.append(f'{field} uses unknown length-unit code {unit:g}; its numeric value is retained without conversion.')
        if active and result != pixels:
            self.warnings.append(f'{field} converts to {pixels:g} px, outside the supported range; {result:g} px is used and the source measurement is retained.')
        self.fields[target] = {
            'source_field': field, 'unit_field': field + 'Unit',
            'source_value': value, 'source_unit': unit,
            'unit': 'mm' if unit == 2 else 'px' if unit == 0 else 'unknown',
            'pixels': result, 'active': bool(active),
            'minimum': minimum, 'maximum': maximum,
        }
        return result

    def metadata(self) -> dict:
        return {'dpi': self.dpi, 'fields': self.fields,
                'has_millimeters': any(record['unit'] == 'mm' and record['source_value'] != 0
                                       and record['active'] for record in self.fields.values()),
                'warnings': list(self.warnings)}


def has_physical_lengths(definition: BrushDefinition) -> bool:
    """Whether active main/secondary imported lengths depend on resolution."""
    return bool(definition.source.get('length_units', {}).get('has_millimeters')
                or (definition.dual is not None and has_physical_lengths(definition.dual)))


def with_import_dpi(definition: BrushDefinition, dpi: float) -> BrushDefinition:
    """Re-resolve original measurements after a DPI choice without decoding tips.

    This is for a prepared import, before publishing it to the preset library.
    It never scales an already converted value, mutates the source, or writes
    the brush file. Independently authored secondary units are resolved too.
    """
    dpi = validate_import_dpi(dpi)
    old = definition.source.get('length_units')
    dual = with_import_dpi(definition.dual, dpi) if definition.dual else None
    if not old:
        return replace(definition, dual=dual)
    conversion = BrushLengthConversion(dpi)
    values = {target: conversion.length(
        target, record['source_field'], record['source_value'], record['source_unit'],
        active=record['active'], minimum=record['minimum'], maximum=record['maximum'])
        for target, record in old['fields'].items()}
    source = dict(definition.source, length_units=conversion.metadata())
    obsolete = set(old.get('warnings', ()))
    if definition.dual:
        obsolete.update(definition.dual.source.get('length_units', {}).get('warnings', ()))
    warnings = [warning for warning in definition.warnings if warning not in obsolete]
    warnings.extend(conversion.warnings)
    if dual:
        warnings.extend(dual.warnings)
    return replace(definition, source=source, dual=dual,
                   warnings=tuple(dict.fromkeys(warnings)), **values)
