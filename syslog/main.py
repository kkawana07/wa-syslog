"""
BSD 3-Clause License

Copyright (c) 2021, Netskope OSS
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its
   contributors may be used to endorse or promote products derived from
   this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""

"""Syslog Plugin."""


import logging
import logging.handlers
import threading
import socket
import json
from typing import List
from jsonpath import jsonpath

from netskope.common.utils import AlertsHelper
from netskope.integrations.cls.plugin_base import (
    PluginBase,
    ValidationResult,
    PushResult,
)
from .utils.syslog_constants import (
    SYSLOG_FORMATS,
    SYSLOG_PROTOCOLS,
)
from .utils.syslog_validator import (
    SyslogValidator,
)
from .utils.syslog_helper import (
    get_syslog_mappings,
)
from .utils.syslog_exceptions import (
    MappingValidationError,
    EmptyExtensionError,
    FieldNotFoundError,
)
from .utils.syslog_cef_generator import (
    CEFGenerator,
)
from .utils.syslog_ssl import SSLSysLogHandler


class SyslogPlugin(PluginBase):
    """The Syslog plugin implementation class."""

    def get_mapping_value_from_json_path(self, data, json_path):
        """To Fetch the value from given JSON object using given JSON path.

        Args:
            data: JSON object from which the value is to be fetched
            json_path: JSON path indicating the path of the value in given JSON

        Returns:
            fetched value.
        """
        return jsonpath(data, json_path)

    def get_mapping_value_from_field(self, data, field):
        """To Fetch the value from given field.

        Args:
            data: JSON object from which the value is to be fetched
            field: Field whose value is to be fetched

        Returns:
            fetched value.
        """
        return (
            data[field]
            if data[field] or isinstance(data[field], int)
            else "null"
        )

    def get_subtype_mapping(self, mappings, subtype):
        """To Retrieve subtype mappings (mappings for subtypes of alerts/events) case insensitively.

        Args:
            mappings: Mapping JSON from which subtypes are to be retrieved
            subtype: Subtype (e.g. DLP for alerts) for which the mapping is to be fetched

        Returns:
            Fetched mapping JSON object
        """
        mappings = {k.lower(): v for k, v in mappings.items()}
        if subtype.upper() in mappings:
            return mappings[subtype.upper()]
        else:
            return mappings[subtype.lower()]

    def get_headers(self, header_mappings, data, data_type, subtype):
        """To Create a dictionary of CEF headers from given header mappings for given Netskope alert/event record.

        Args:
            subtype: Subtype for which the headers are being transformed
            data_type: Data type for which the headers are being transformed
            header_mappings: CEF header mapping with Netskope fields
            data: The alert/event for which the CEF header is being generated

        Returns:
            header dict
        """
        headers = {}
        mapping_variables = {}
        if data_type != 'webtx':
            helper = AlertsHelper()
            tenant = helper.get_tenant_cls(self.source)
            mapping_variables = {"$tenant_name": tenant.name}

        missing_fields = []
        # Iterate over mapped headers
        for cef_header, header_mapping in header_mappings.items():
            try:
                headers[cef_header] = self.get_field_value_from_data(
                    header_mapping, data, data_type, subtype, False
                )

                # Handle variable mappings
                if (
                    isinstance(headers[cef_header], str)
                    and headers[cef_header].upper() in mapping_variables
                ):
                    headers[cef_header] = mapping_variables[
                        headers[cef_header].upper()
                    ]
            except FieldNotFoundError as err:
                missing_fields.append(str(err))

        return headers

    def get_extensions(self, extension_mappings, data, data_type, subtype):
        """Fetch extensions from given mappings.

        Args:
            subtype: Subtype for which the headers are being transformed
            data_type: Data type for which the headers are being transformed
            extension_mappings: Mapping of extensions
            data: The data to be transformed

        Returns:
            extensions (dict)
        """
        extension = {}
        missing_fields = []

        # Iterate over mapped extensions
        for cef_extension, extension_mapping in extension_mappings.items():
            try:
                extension[cef_extension] = self.get_field_value_from_data(
                    extension_mapping,
                    data,
                    data_type,
                    subtype,
                    is_json_path="is_json_path" in extension_mapping,
                )
            except FieldNotFoundError as err:
                missing_fields.append(str(err))

        return extension

    def get_field_value_from_data(
        self, extension_mapping, data, data_type, subtype, is_json_path=False
    ):
        """To Fetch the value of extension based on "mapping" and "default" fields.

        Args:
            extension_mapping: Dict containing "mapping" and "default" fields
            data: Data instance retrieved from Netskope
            subtype: Subtype for which the extension are being transformed
            data_type: Data type for which the headers are being transformed
            is_json_path: Whether the mapped value is JSON path or direct field name

        Returns:
            Fetched values of extension

        ---------------------------------------------------------------------
             Mapping          |    Response    |    Retrieved Value
        ----------------------|                |
        default  |  Mapping   |                |
        ---------------------------------------------------------------------
           P     |     P      |        P       |           Mapped
           P     |     P      |        NP      |           Default
           P     |     NP     |        P       |           Default
           NP    |     P      |        P       |           Mapped
           P     |     NP     |        NP      |           Default
           NP    |     P      |        NP      |           -
           NP    |     NP     |        P       |           - (Not possible)
           NP    |     NP     |        NP      |           - (Not possible)
        -----------------------------------------------------------------------
        """
        if (
            "mapping_field" in extension_mapping
            and extension_mapping["mapping_field"]
        ):
            if is_json_path:
                # If mapping field specified by JSON path is present in data, map that field, else skip by raising
                # exception:
                value = self.get_mapping_value_from_json_path(
                    data, extension_mapping["mapping_field"]
                )
                if value:
                    return ",".join([str(val) for val in value])
                else:
                    raise FieldNotFoundError(
                        extension_mapping["mapping_field"]
                    )
            else:
                # If mapping is present in data, map that field, else skip by raising exception
                if (
                    extension_mapping["mapping_field"] in data
                ):  # case #1 and case #4
                    return self.get_mapping_value_from_field(
                        data, extension_mapping["mapping_field"]
                    )
                elif "default_value" in extension_mapping:
                    # If mapped value is not found in response and default is mapped, map the default value (case #2)
                    return extension_mapping["default_value"]
                else:  # case #6
                    raise FieldNotFoundError(
                        extension_mapping["mapping_field"]
                    )
        else:
            # If mapping is not present, 'default_value' must be there because of validation (case #3 and case #5)
            return extension_mapping["default_value"]

    def transform(self, raw_data, data_type, subtype) -> List:
        """To Transform the raw netskope JSON data into target platform supported data formats."""
        try:
            delimiter, cef_version, syslog_mappings = get_syslog_mappings(
                self.mappings, data_type
            )
        except KeyError as err:
            self.logger.error(
                "Error in syslog mapping file. Error: {}".format(str(err))
            )
            raise
        except MappingValidationError as err:
            self.logger.error(str(err))
            raise
        except Exception as err:
            self.logger.error(
                "An error occurred while mapping data using given json mappings. Error: {}".format(
                    str(err)
                )
            )
            raise

        cef_generator = CEFGenerator(
            self.mappings,
            delimiter,
            cef_version,
            self.logger,
        )

        transformed_data = []
        for data in raw_data:

            # First retrieve the mapping of subtype being transformed
            try:
                subtype_mapping = self.get_subtype_mapping(
                    syslog_mappings[data_type], subtype
                )
            except Exception:
                self.logger.error(
                    'Error occurred while retrieving mappings for subtype "{}". '
                    "Transformation of current record will be skipped.".format(
                        subtype
                    )
                )
                continue

            # Generating the CEF header
            try:
                header = self.get_headers(
                    subtype_mapping["header"], data, data_type, subtype
                )
            except Exception as err:
                self.logger.error(
                    "[{}][{}]: Error occurred while creating CEF header: {}. Transformation of "
                    "current record will be skipped.".format(
                        data_type, subtype, str(err)
                    )
                )
                continue

            try:
                extension = self.get_extensions(
                    subtype_mapping["extension"], data, data_type, subtype
                )
            except Exception as err:
                self.logger.error(
                    "[{}][{}]: Error occurred while creating CEF extension: {}. Transformation of "
                    "the current record will be skipped".format(
                        data_type, subtype, str(err)
                    )
                )
                continue

            try:
                transformed_data.append(
                    cef_generator.get_cef_event(
                        data, header, extension, data_type, subtype
                    )
                )
            except EmptyExtensionError:
                self.logger.error(
                    "[{}][{}]: Got empty extension during transformation."
                    "Transformation of current record will be skipped".format(
                        data_type, subtype
                    )
                )
            except Exception as err:
                self.logger.error(
                    "[{}][{}]: An error occurred during transformation."
                    " Error: {}".format(data_type, subtype, str(err))
                )
        return transformed_data

    def init_handler(self, configuration):
        """Initialize unique Syslog handler per thread based on configured protocol."""
        syslogger = logging.getLogger(
            "SYSLOG_LOGGER_{}".format(threading.get_ident())
        )
        syslogger.setLevel(logging.INFO)
        syslogger.handlers = []
        syslogger.propagate = False

        if configuration["syslog_protocol"] == "TLS":
            tls_handler = SSLSysLogHandler(
                address=(
                    configuration["syslog_server"],
                    configuration["syslog_port"],
                ),
                certs=configuration["syslog_certificate"],
            )
            syslogger.addHandler(tls_handler)
        else:
            socktype = socket.SOCK_DGRAM  # Set protocol to UDP by default
            if configuration["syslog_protocol"] == "TCP":
                socktype = socket.SOCK_STREAM

            # Create a syslog handler with given configuration parameters
            handler = logging.handlers.SysLogHandler(
                address=(
                    configuration["syslog_server"],
                    configuration["syslog_port"],
                ),
                socktype=socktype,
            )

            if configuration["syslog_protocol"] == "TCP":
                # This will add a line break to the message before it is 'emitted' which ensures that the messages are
                # split up over multiple lines, see https://bugs.python.org/issue28404
                handler.setFormatter(logging.Formatter("%(message)s\n"))
                # In order for the above to work, then we need to ensure that the null terminator is not included
                handler.append_nul = False

            syslogger.addHandler(handler)

        return syslogger

    def push(self, transformed_data, data_type, subtype) -> PushResult:
        """Push the transformed_data to the 3rd party platform."""
        try:
            syslogger = self.init_handler(self.configuration)
        except Exception as err:
            self.logger.error(
                "Error occurred during initializing connection. Error: {}".format(
                    str(err)
                )
            )
            raise

        # Log the transformed data to given syslog server
        for data in transformed_data:
            try:
                syslogger.info(data)
                syslogger.handlers[0].flush()
            except Exception as err:
                self.logger.error(
                    "Error occurred during data ingestion."
                    " Error: {}. Record will be skipped".format(str(err))
                )

        # Clean up
        try:
            syslogger.handlers[0].close()
            del syslogger.handlers[:]
            del syslogger
        except Exception as err:
            self.logger.error(
                "Error occurred during Clean up. Error: {}".format(str(err))
            )

    def test_server_connectivity(self, configuration):
        """Tests whether the configured syslog server is reachable or not."""
        try:
            syslogger = self.init_handler(configuration)
        except Exception as err:
            self.logger.error(
                "Error occurred while establishing connection with syslog server. Make sure "
                "you have provided correct syslog server and port."
            )
            raise err
        else:
            # Clean up for further use
            syslogger.handlers[0].flush()
            syslogger.handlers[0].close()
            del syslogger.handlers[:]
            del syslogger

    def validate(self, configuration: dict) -> ValidationResult:
        """Validate the configuration parameters dict."""
        syslog_validator = SyslogValidator(self.logger)

        if (
            "syslog_server" not in configuration
            or type(configuration["syslog_server"]) != str
            or not configuration["syslog_server"].strip()
        ):
            self.logger.error(
                "Syslog Plugin: Validation error occurred. Error: "
                "Invalid Syslog server IP/FQDN found in the configuration parameters."
            )
            return ValidationResult(
                success=False, message="Invalid Syslog server provided."
            )

        if (
            "syslog_format" not in configuration
            or type(configuration["syslog_format"]) != str
            or not configuration["syslog_format"].strip()
            or configuration["syslog_format"] not in SYSLOG_FORMATS
        ):
            self.logger.error(
                "Syslog Plugin: Validation error occurred. Error: "
                "Invalid Syslog format found in the configuration parameters."
            )
            return ValidationResult(
                success=False, message="Invalid Syslog format provided."
            )

        if (
            "syslog_protocol" not in configuration
            or type(configuration["syslog_protocol"]) != str
            or not configuration["syslog_protocol"].strip()
            or configuration["syslog_protocol"] not in SYSLOG_PROTOCOLS
        ):
            self.logger.error(
                "Syslog Plugin: Validation error occurred. Error: "
                "Invalid Syslog protocol found in the configuration parameters."
            )
            return ValidationResult(
                success=False, message="Invalid Syslog protocol provided."
            )

        if (
            "syslog_port" not in configuration
            or not configuration["syslog_port"]
            or not syslog_validator.validate_syslog_port(
                configuration["syslog_port"]
            )
        ):
            self.logger.error(
                "Syslog Plugin: Validation error occurred. Error: "
                "Invalid Syslog port found in the configuration parameters."
            )
            return ValidationResult(
                success=False, message="Invalid Syslog port provided."
            )

        mappings = self.mappings.get("jsonData", None)
        mappings = json.loads(mappings)
        if type(mappings) != dict or not syslog_validator.validate_syslog_map(
            mappings
        ):
            self.logger.error(
                "Syslog Plugin: Validation error occurred. Error: "
                "Invalid Syslog attribute mapping found in the configuration parameters."
            )
            return ValidationResult(
                success=False,
                message="Invalid Syslog attribute mapping provided.",
            )

        if configuration["syslog_protocol"].upper() == "TLS" and (
            "syslog_certificate" not in configuration
            or type(configuration["syslog_certificate"]) != str
            or not configuration["syslog_certificate"].strip()
        ):
            self.logger.error(
                "Syslog Plugin: Validation error occurred. Error: "
                "Invalid Syslog certificate mapping found in the configuration parameters."
            )
            return ValidationResult(
                success=False,
                message="Invalid Syslog certificate mapping provided.",
            )

        # Validate Server connection.
        try:
            self.test_server_connectivity(configuration)
        except Exception:
            self.logger.error(
                "Syslog Plugin: Validation error occurred. Error: "
                "Connection to SIEM platform is not established."
            )
            return ValidationResult(
                success=False,
                message="Error occurred while establishing connection with Syslog server. "
                "Make sure you have provided correct Syslog Server, Port and Syslog Certificate(if required).",
            )

        return ValidationResult(success=True, message="Validation successful.")

    def chunk_size(self):
        """Chunk size to be ingested per thread."""
        return 2000
