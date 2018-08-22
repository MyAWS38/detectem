import logging
import urllib.parse
import hashlib

from detectem.utils import (
    get_most_complete_version,
    get_url,
    get_response_body,
)
from detectem.settings import (
    INDICATOR_TYPE,
    HINT_TYPE,
    MAIN_ENTRY,
    RESOURCE_ENTRY,
    INLINE_SCRIPT_ENTRY,
    GENERIC_TYPE,
)
from detectem.matchers import (UrlMatcher, BodyMatcher, HeaderMatcher, XPathMatcher)
from detectem.results import Result, ResultCollection

logger = logging.getLogger('detectem')
MATCHERS = {
    'url': UrlMatcher(),
    'body': BodyMatcher(),
    'header': HeaderMatcher(),
    'xpath': XPathMatcher(),
}


class Detector():
    def __init__(self, response, plugins, requested_url):
        self.requested_url = requested_url
        self.har = self._prepare_har(response)

        self._softwares_from_splash = response['softwares']
        self._plugins = plugins
        self._results = ResultCollection()

    def _prepare_har(self, response):
        har = response.get('har', [])
        if har:
            self._mark_main_entry(har)
        for script in response.get('scripts', []):
            har.append(self._script_to_har_entry(script))
        return har

    def _mark_main_entry(self, entries):
        for entry in entries:
            self._set_entry_type(entry, RESOURCE_ENTRY)

        def get_url(entry):
            return entry['request']['url']

        def get_location(entry):
            headers = entry['response'].get('headers', [])
            for header in headers:
                if header['name'] == 'Location':
                    return header['value']
            return None

        main_entry = entries[0]
        main_location = get_location(main_entry)
        if not main_location:
            self._set_entry_type(main_entry, MAIN_ENTRY)
            return
        main_url = urllib.parse.urljoin(get_url(main_entry), main_location)

        for entry in entries[1:]:
            url = get_url(entry)
            if url == main_url:
                self._set_entry_type(entry, MAIN_ENTRY)
                break
        else:
            self._set_entry_type(main_entry, MAIN_ENTRY)

    def _script_to_har_entry(self, script):
        entry = {
            'request': {'url': self.requested_url},
            'response': {'url': self.requested_url, 'content': {'text': script}}
        }
        self._set_entry_type(entry, INLINE_SCRIPT_ENTRY)
        return entry

    @staticmethod
    def _set_entry_type(entry, entry_type):
        entry.setdefault('detectem', {})['type'] = entry_type

    @staticmethod
    def _get_entry_type(entry):
        return entry['detectem']['type']

    def get_hints(self, plugin):
        """ Get plugins hints from `plugin` on `entry`.

        Plugins hints return `Result` or `None`.

        """
        hints = []

        for hint_name in getattr(plugin, 'hints', []):
            hint_plugin = self._plugins.get(hint_name)
            if hint_plugin:
                hint_result = Result(
                    name=hint_plugin.name,
                    homepage=hint_plugin.homepage,
                    from_url=self.requested_url,
                    type=HINT_TYPE,
                )
                hints.append(hint_result)
                logger.debug(
                    '%(pname)s & hint %(hname)s detected',
                    {'pname': plugin.name, 'hname': hint_result.name}
                )
            else:
                logger.error(
                    '%(pname)s hints an invalid plugin: %(hname)s',
                    {'pname': plugin.name, 'hname': hint_name}
                )

        return hints

    def process_from_splash(self):
        for software in self._softwares_from_splash:
            plugin = self._plugins.get(software['name'])
            self._results.add_result(
                Result(
                    name=plugin.name,
                    version=software['version'],
                    homepage=plugin.homepage,
                    from_url=self.requested_url,
                )
            )
            for hint in self.get_hints(plugin):
                self._results.add_result(hint)

    def process_har(self):
        """ Detect plugins present in the page.

        First, start with version plugins, then software from Splash
        and finish with indicators.
        In each phase try to detect plugin hints in already detected plugins.

        """
        hints = []

        version_plugins = self._plugins.with_version_matchers()
        indicator_plugins = self._plugins.with_indicator_matchers()
        generic_plugins = self._plugins.with_generic_matchers()

        for entry in self.har:
            for plugin in version_plugins:
                version = self.get_plugin_version(plugin, entry)
                if version:
                    # Name could be different than plugin name in modular plugins
                    name = self.get_plugin_name(plugin, entry)
                    self._results.add_result(
                        Result(
                            name=name,
                            version=version,
                            homepage=plugin.homepage,
                            from_url=get_url(entry),
                        )
                    )
                    hints += self.get_hints(plugin)

            for plugin in indicator_plugins:
                is_present = self.check_indicator_presence(plugin, entry)
                if is_present:
                    name = self.get_plugin_name(plugin, entry)
                    self._results.add_result(
                        Result(
                            name=name,
                            homepage=plugin.homepage,
                            from_url=get_url(entry),
                            type=INDICATOR_TYPE,
                        )
                    )
                    hints += self.get_hints(plugin)

                    # Try to get version through file hashes
                    version = self.get_version_via_file_hashes(plugin, entry)
                    if version:
                        self._results.add_result(
                            Result(
                                name=name,
                                version=version,
                                homepage=plugin.homepage,
                                from_url=get_url(entry),
                            )
                        )
                    else:
                        self._results.add_result(
                            Result(
                                name=name,
                                homepage=plugin.homepage,
                                from_url=get_url(entry),
                                type=INDICATOR_TYPE,
                            )
                        )

            for plugin in generic_plugins:
                is_present = self.check_indicator_presence(plugin, entry)
                if is_present:
                    plugin_data = plugin.get_information(entry)

                    # Only add to results if it's a valid result
                    if 'name' in plugin_data:
                        self._results.add_result(
                            Result(
                                name=plugin_data['name'],
                                homepage=plugin_data['homepage'],
                                from_url=get_url(entry),
                                type=GENERIC_TYPE,
                            )
                        )

        for hint in hints:
            self._results.add_result(hint)

    def get_results(self, metadata=False):
        """ Return results of the analysis. """
        results_data = []

        self.process_har()
        self.process_from_splash()

        for rt in sorted(self._results.get_results()):
            rdict = {'name': rt.name}
            if rt.version:
                rdict['version'] = rt.version

            if metadata:
                rdict['homepage'] = rt.homepage
                rdict['type'] = rt.type
                rdict['from_url'] = rt.from_url

            results_data.append(rdict)

        return results_data

    def _get_matchers_for_entry(self, source, plugin, entry):
        grouped_matchers = plugin.get_grouped_matchers(source)

        def remove_group(group):
            if group in grouped_matchers:
                del grouped_matchers[group]

        if self._get_entry_type(entry) == MAIN_ENTRY:
            remove_group('body')
            remove_group('url')
        else:
            remove_group('header')
            remove_group('xpath')

        return grouped_matchers

    def get_plugin_version(self, plugin, entry):
        """ Return version after applying proper ``plugin`` matchers to ``entry``.

        The matchers could return many versions, but at the end one is returned.

        """
        versions = []
        grouped_matchers = self._get_matchers_for_entry('matchers', plugin, entry)

        for key, matchers in grouped_matchers.items():
            klass = MATCHERS[key]
            version = klass.get_version(entry, *matchers)
            if version:
                versions.append(version)

        return get_most_complete_version(versions)

    def get_version_via_file_hashes(self, plugin, entry):
        file_hashes = getattr(plugin, 'file_hashes', {})
        if not file_hashes:
            return

        url = get_url(entry)
        body = get_response_body(entry).encode('utf-8')
        for file, hash_dict in file_hashes.items():
            if file not in url:
                continue

            m = hashlib.sha256()
            m.update(body)
            h = m.hexdigest()

            for version, version_hash in hash_dict.items():
                if h == version_hash:
                    return version

    def get_plugin_name(self, plugin, entry):
        """ Return plugin name with module name if it's found.
        Otherwise return the normal plugin name.

        """
        if not plugin.is_modular:
            return plugin.name

        grouped_matchers = self._get_matchers_for_entry('modular_matchers', plugin, entry)
        module_name = None

        for key, matchers in grouped_matchers.items():
            klass = MATCHERS[key]
            module_name = klass.get_module_name(entry, *matchers)
            if module_name:
                break

        if module_name:
            name = '{}-{}'.format(plugin.name, module_name)
        else:
            name = plugin.name

        return name

    def check_indicator_presence(self, plugin, entry):
        """ Return presence after applying proper ``plugin`` matchers to ``entry``.

        The matchers return boolean values and at least one is enough
        to assert the presence of the plugin.

        """
        grouped_matchers = self._get_matchers_for_entry('indicators', plugin, entry)
        presences = []

        for key, matchers in grouped_matchers.items():
            klass = MATCHERS[key]
            presence = klass.check_presence(entry, *matchers)
            presences.append(presence)

        return any(presences)
