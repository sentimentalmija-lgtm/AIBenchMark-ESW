"""Compiler-independent target syntax shared by execution and task loading."""

import re


def supported_target(value):
    return isinstance(value, str) and re.fullmatch(
        r'(?:avr:atmega[0-9]+[a-z]*|arm:cortex-m(?:0plus|[0347]|23|33|55|85)|riscv:rv(?:32(?:i|imc|imac|ec)|64(?:gc|imac)))',
        value) is not None


def supported_target_selector(value):
    return isinstance(value, str) and (supported_target(value) or any(
        supported_target(architecture + ':' + value) for architecture in ('avr', 'arm', 'riscv')))
