"""Coverage plugin for pytest."""
import os

import pytest
import argparse
from coverage.misc import CoverageException

from . import embed
from . import engine
from . import compat


class CoverageError(Exception):
    """Indicates that our coverage is too low"""


def validate_report(arg):
    file_choices = ['annotate', 'html', 'xml']
    term_choices = ['term', 'term-missing']
    all_choices = term_choices + file_choices
    values = arg.split(":", 1)
    report_type = values[0]
    if report_type not in all_choices + ['']:
        msg = 'invalid choice: "{}" (choose from "{}")'.format(arg, all_choices)
        raise argparse.ArgumentTypeError(msg)

    if len(values) == 1:
        return report_type, None

    if report_type not in file_choices:
        msg = 'output specifier not supported for: "{}" (choose from "{}")'.format(arg,
                                                                                   file_choices)
        raise argparse.ArgumentTypeError(msg)

    return values


class StoreReport(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        report_type, file = values
        namespace.cov_report[report_type] = file


def pytest_addoption(parser):
    """Add options to control coverage."""

    group = parser.getgroup(
        'cov', 'coverage reporting with distributed testing support')
    group.addoption('--cov', action='append', default=[], metavar='path',
                    nargs='?', const=True, dest='cov_source',
                    help='measure coverage for filesystem path '
                    '(multi-allowed)')
    group.addoption('--cov-report', action=StoreReport, default={},
                    metavar='type', type=validate_report,
                    help='type of report to generate: term, term-missing, '
                    'annotate, html, xml (multi-allowed). '
                    'annotate, html and xml may be be followed by ":DEST" '
                    'where DEST specifies the output location.')
    group.addoption('--cov-config', action='store', default='.coveragerc',
                    metavar='path',
                    help='config file for coverage, default: .coveragerc')
    group.addoption('--no-cov-on-fail', action='store_true', default=False,
                    help='do not report coverage if test run fails, '
                         'default: False')
    group.addoption('--cov-fail-under', action='store', metavar='MIN', type='int',
                    help='Fail if the total coverage is less than MIN.')
    group.addoption('--cov-append', action='store_true', default=False,
                    help='do not delete coverage but append to current, '
                         'default: False')


@pytest.mark.tryfirst
def pytest_load_initial_conftests(early_config, parser, args):
    ns = parser.parse_known_args(args)
    ns.cov = bool(ns.cov_source)

    if ns.cov_source == [True]:
        ns.cov_source = None

    if not ns.cov_report:
        ns.cov_report = ['term']
    elif len(ns.cov_report) == 1 and '' in ns.cov_report:
        ns.cov_report = {}

    if ns.cov:
        plugin = CovPlugin(ns, early_config.pluginmanager)
        early_config.pluginmanager.register(plugin, '_cov')


def pytest_configure(config):
    """Activate coverage plugin if appropriate."""
    if config.getvalue('cov_source'):
        if not config.pluginmanager.hasplugin('_cov'):
            if not config.option.cov_report:
                config.option.cov_report = ['term']
            if config.option.cov_source == [True]:
                config.option.cov_source = None

            plugin = CovPlugin(config.option, config.pluginmanager,
                               start=False)
            config.pluginmanager.register(plugin, '_cov')


class CovPlugin(object):
    """Use coverage package to produce code coverage reports.

    Delegates all work to a particular implementation based on whether
    this test process is centralised, a distributed master or a
    distributed slave.
    """

    def __init__(self, options, pluginmanager, start=True):
        """Creates a coverage pytest plugin.

        We read the rc file that coverage uses to get the data file
        name.  This is needed since we give coverage through it's API
        the data file name.
        """

        # Our implementation is unknown at this time.
        self.pid = None
        self.cov = None
        self.cov_controller = None
        self.cov_report = compat.StringIO()
        self.cov_total = None
        self.failed = False
        self._started = False
        self.options = options

        is_dist = (getattr(options, 'numprocesses', False) or
                   getattr(options, 'distload', False) or
                   getattr(options, 'dist', 'no') != 'no')
        if is_dist and start:
            self.start(engine.DistMaster)
        elif start:
            self.start(engine.Central)

        # slave is started in pytest hook

    def start(self, controller_cls, config=None, nodeid=None):
        if config is None:
            # fake config option for engine
            class Config(object):
                option = self.options

            config = Config()

        self.cov_controller = controller_cls(
            self.options.cov_source,
            self.options.cov_report,
            self.options.cov_config,
            self.options.cov_append,
            config,
            nodeid
        )
        self.cov_controller.start()
        self._started = True
        cov_config = self.cov_controller.cov.config
        if self.options.cov_fail_under is None and hasattr(cov_config, 'fail_under'):
            self.options.cov_fail_under = cov_config.fail_under

    def pytest_sessionstart(self, session):
        """At session start determine our implementation and delegate to it."""
        self.pid = os.getpid()
        is_slave = hasattr(session.config, 'slaveinput')
        if is_slave:
            nodeid = session.config.slaveinput.get('slaveid',
                                                   getattr(session, 'nodeid'))
            self.start(engine.DistSlave, session.config, nodeid)
        elif not self._started:
            self.start(engine.Central)

    def pytest_configure_node(self, node):
        """Delegate to our implementation.

        Mark this hook as optional in case xdist is not installed.
        """
        self.cov_controller.configure_node(node)
    pytest_configure_node.optionalhook = True

    def pytest_testnodedown(self, node, error):
        """Delegate to our implementation.

        Mark this hook as optional in case xdist is not installed.
        """
        self.cov_controller.testnodedown(node, error)
    pytest_testnodedown.optionalhook = True

    def _should_report(self):
        return not (self.failed and self.options.no_cov_on_fail)

    def _failed_cov_total(self):
        cov_fail_under = self.options.cov_fail_under
        return cov_fail_under is not None and self.cov_total < cov_fail_under

    # we need to wrap pytest_runtestloop. by the time pytest_sessionfinish
    # runs, it's too late to set testsfailed
    @compat.hookwrapper
    def pytest_runtestloop(self, session):
        yield

        compat_session = compat.SessionWrapper(session)

        self.failed = bool(compat_session.testsfailed)
        if self.cov_controller is not None:
            self.cov_controller.finish()

        if self._should_report():
            try:
                self.cov_total = self.cov_controller.summary(self.cov_report)
            except CoverageException as exc:
                raise pytest.UsageError(
                    'Failed to generate report: %s\n' % exc
                )
            assert self.cov_total is not None, 'Test coverage should never be `None`'
            if self._failed_cov_total():
                # make sure we get the EXIT_TESTSFAILED exit code
                compat_session.testsfailed += 1

    def pytest_terminal_summary(self, terminalreporter):
        if self.cov_controller is None:
            return

        if self.cov_total is None:
            # we shouldn't report, or report generation failed (error raised above)
            return

        terminalreporter.write('\n' + self.cov_report.getvalue() + '\n')

        if self._failed_cov_total():
            markup = {'red': True, 'bold': True}
            msg = (
                'FAIL Required test coverage of %d%% not '
                'reached. Total coverage: %.2f%%\n'
                % (self.options.cov_fail_under, self.cov_total)
            )
            terminalreporter.write(msg, **markup)

    def pytest_runtest_setup(self, item):
        if os.getpid() != self.pid:
            # test is run in another process than session, run
            # coverage manually
            self.cov = embed.init()

    def pytest_runtest_teardown(self, item):
        if self.cov is not None:
            embed.multiprocessing_finish(self.cov)
            self.cov = None


def pytest_funcarg__cov(request):
    """A pytest funcarg that provides access to the underlying coverage
    object.
    """

    # Check with hasplugin to avoid getplugin exception in older pytest.
    if request.config.pluginmanager.hasplugin('_cov'):
        plugin = request.config.pluginmanager.getplugin('_cov')
        if plugin.cov_controller:
            return plugin.cov_controller.cov
    return None
