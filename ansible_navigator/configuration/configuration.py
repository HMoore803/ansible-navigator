""" The configuration object
"""
import os

from copy import deepcopy
from typing import List
from typing import Union

from .definitions import Config
from .definitions import EntrySource
from .definitions import Message
from .parser import Parser

from ..utils import error_and_exit_early
from ..yaml import SafeLoader
from ..yaml import yaml


class Configuration:
    # pylint: disable=too-many-arguments
    # pylint: disable=too-few-public-methods
    """the configuration class"""

    def __init__(
        self,
        params: List[str],
        application_configuration: Config,
        apply_previous_cli_entries: Union[List, None] = None,
        save_as_intitial: bool = False,
        settings_file_path: str = None,
    ):
        """
        :param params: A list of parameters ie ['-x', 'value']
        :param application_configuration: An application specific Config object
        :param apply_previous_cli_entries: Apply previous USER_CLI values where the current value
                                           is not a USER_CLI sourced value, a list of entry names
                                           ['all'] will apply all previous
        :param save_as_initial: Save the resulting configuration as the 'initial' configuration
                                The 'initial' will be used as a source for apply_previous_cli
        :param settings_file_path: The full path to a settings file
        """
        self._apply_previous_cli_entries = apply_previous_cli_entries
        self._config = application_configuration
        self._errors: List[str] = []
        self._messages: List[Message] = []
        self._params = params
        self._save_as_intitial = save_as_intitial
        self._settings_file_path = settings_file_path
        self._sanity_check()

    def _sanity_check(self) -> None:
        if self._apply_previous_cli_entries is not None:
            if self._save_as_intitial is True:
                raise ValueError("'apply_previous_cli' cannot be used with 'save_as_initial'")
            if self._config.initial is None:
                raise ValueError("'apply_previous_cli' enabled prior to 'save_as_initial'")

    def configure(self) -> List[Message]:
        """Perform the configuration"""
        self._apply_defaults()
        self._apply_settings_file()
        self._apply_environment_variables()
        self._apply_cli_params()
        if self._errors:
            error_and_exit_early(errors=self._errors)

        self._post_process()
        if self._errors:
            error_and_exit_early(errors=self._errors)

        self._check_choices()
        if self._errors:
            error_and_exit_early(errors=self._errors)

        self._apply_previous_cli_to_current()

        if self._save_as_intitial:
            self._config.initial = deepcopy(self._config)

        return self._messages

    def _apply_defaults(self) -> None:
        for entry in self._config.entries:
            entry.value.current = entry.value.default
            entry.value.source = EntrySource.DEFAULT_CFG

    def _apply_settings_file(self) -> None:
        if self._settings_file_path:
            with open(self._settings_file_path, "r") as config_fh:
                try:
                    config = yaml.load(config_fh, Loader=SafeLoader)
                except (yaml.scanner.ScannerError, yaml.parser.ParserError):
                    msg = f"Settings file found {self._settings_file_path}, but failed to load it."
                    self._errors.append(msg)
                    return
            for entry in self._config.entries:
                settings_file_path = entry.settings_file_path(self._config.application_name)
                path_parts = settings_file_path.split(".")
                data = config
                try:
                    for key in path_parts:
                        data = data[key]
                    entry.value.current = data
                    entry.value.source = EntrySource.USER_CFG
                except TypeError:
                    msg = f"{self._settings_file_path} empty"
                    self._errors.append(msg)
                    return
                except KeyError:
                    msg = f"{settings_file_path} not found in settings file"
                    self._messages.append(Message(log_level="debug", message=msg))

    def _apply_environment_variables(self) -> None:
        for entry in self._config.entries:
            set_envvar = os.environ.get(entry.environment_variable(self._config.application_name))
            if set_envvar is not None:
                if entry.cli_parameters is not None and entry.cli_parameters.nargs == "+":
                    entry.value.current = set_envvar.split(",")
                else:
                    entry.value.current = set_envvar
                entry.value.source = EntrySource.ENVIRONMENT_VARIABLE

    def _apply_cli_params(self) -> None:
        parser = Parser(self._config).parser
        args, cmdline = parser.parse_known_args(self._params)
        if cmdline:
            self._config.entry("cmdline").value.current = cmdline
            self._config.entry("cmdline").value.source = EntrySource.USER_CLI
        for param, value in vars(args).items():
            if self._config.entry(param).subcommand_value is True and value is None:
                continue
            self._config.entry(param).value.current = value
            self._config.entry(param).value.source = EntrySource.USER_CLI

    def _post_process(self) -> None:
        for entry in self._config.entries:
            processor = getattr(self._config.post_processor, entry.name, None)
            if callable(processor):
                messages, errors = processor(entry=entry, config=self._config)
                self._messages.extend(messages)
                self._errors.extend(errors)

    def _check_choices(self) -> None:
        for entry in self._config.entries:
            if entry.cli_parameters and entry.choices:
                if entry.value.current not in entry.choices:
                    self._errors.append(entry.invalid_choice)

    def _apply_previous_cli_to_current(self) -> None:
        # pylint: disable=too-many-nested-blocks
        """
        1) for each current entry
        2) that isn't the subcommand holder
        3) and the source is an EntrySource
        4) and wasn't set by the current cli (USER_CLI)
        5) get the previous entry
        6) if it was set by the previous cli (USER_CLI)
        7) replace the current with the previous
        """
        if self._apply_previous_cli_entries is not None:
            for current_entry in self._config.entries:
                if current_entry.subcommand_value is not True:
                    if isinstance(current_entry.value.source, EntrySource):
                        if current_entry.value.source.name != "USER_CLI":
                            if self._apply_previous_cli_entries == ["all"] or (
                                current_entry.name in self._apply_previous_cli_entries
                            ):
                                previous_entry = self._config.initial.entry(current_entry.name)
                                if previous_entry.value.source.name == "USER_CLI":
                                    current_entry.value.current = previous_entry.value.current
                                    current_entry.value.source = EntrySource.PREVIOUS_CLI