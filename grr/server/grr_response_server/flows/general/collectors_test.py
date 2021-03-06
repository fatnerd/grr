#!/usr/bin/env python
"""Test the collector flows.

To reduce the size of this module, additional collector flow tests are split out
into collectors_*_test.py files.
"""

import os

import mock
import psutil

from grr import config
from grr_response_client.client_actions import standard
from grr.core.grr_response_core.lib import artifact_utils
from grr.core.grr_response_core.lib import flags
from grr.core.grr_response_core.lib import utils
from grr.core.grr_response_core.lib.rdfvalues import artifacts as rdf_artifacts
from grr.core.grr_response_core.lib.rdfvalues import client as rdf_client
from grr.core.grr_response_core.lib.rdfvalues import paths as rdf_paths
from grr.server.grr_response_server import aff4
from grr.server.grr_response_server import artifact
from grr.server.grr_response_server import artifact_registry
from grr.server.grr_response_server import flow
from grr.server.grr_response_server import sequential_collection
from grr.server.grr_response_server.flows.general import collectors
from grr.test_lib import action_mocks
from grr.test_lib import artifact_test_lib
from grr.test_lib import client_test_lib
from grr.test_lib import flow_test_lib
from grr.test_lib import test_lib
from grr.test_lib import vfs_test_lib


def ProcessIter():
  return iter([client_test_lib.MockWindowsProcess()])


class TestArtifactCollectors(flow_test_lib.FlowTestsBaseclass):
  """Test the artifact collection mechanism with fake artifacts."""

  def setUp(self):
    """Make sure things are initialized."""
    super(TestArtifactCollectors, self).setUp()

    self._patcher = artifact_test_lib.PatchDefaultArtifactRegistry()
    self._patcher.start()

    test_artifacts_file = os.path.join(config.CONFIG["Test.data_dir"],
                                       "artifacts", "test_artifacts.json")
    artifact_registry.REGISTRY.AddFileSource(test_artifacts_file)

    self.fakeartifact = artifact_registry.REGISTRY.GetArtifact("FakeArtifact")
    self.fakeartifact2 = artifact_registry.REGISTRY.GetArtifact("FakeArtifact2")

    self.output_count = 0

    self.client_id = self.SetupClient(0)

    with aff4.FACTORY.Open(self.client_id, token=self.token, mode="rw") as fd:
      fd.Set(fd.Schema.SYSTEM("Linux"))
      kb = fd.Schema.KNOWLEDGE_BASE()
      artifact.SetCoreGRRKnowledgeBaseValues(kb, fd)
      fd.Set(kb)

  def tearDown(self):
    self._patcher.stop()
    super(TestArtifactCollectors, self).tearDown()

  def testInterpolateArgs(self):
    collect_flow = collectors.ArtifactCollectorFlow(None, token=self.token)

    kb = rdf_client.KnowledgeBase()
    kb.MergeOrAddUser(rdf_client.User(username="test1"))
    kb.MergeOrAddUser(rdf_client.User(username="test2"))
    collect_flow.state["knowledge_base"] = kb

    collect_flow.current_artifact_name = "blah"
    collect_flow.args = artifact_utils.ArtifactCollectorFlowArgs()

    test_rdf = rdf_client.KnowledgeBase()
    action_args = {
        "usernames": ["%%users.username%%", "%%users.username%%"],
        "nointerp": "asdfsdf",
        "notastring": test_rdf
    }

    kwargs = collect_flow.InterpolateDict(action_args)
    self.assertItemsEqual(kwargs["usernames"],
                          ["test1", "test2", "test1", "test2"])
    self.assertEqual(kwargs["nointerp"], "asdfsdf")
    self.assertEqual(kwargs["notastring"], test_rdf)

    # We should be using an array since users.username will expand to multiple
    # values.
    self.assertRaises(ValueError, collect_flow.InterpolateDict,
                      {"bad": "%%users.username%%"})

    list_args = collect_flow.InterpolateList(
        ["%%users.username%%", r"%%users.username%%\aa"])
    self.assertItemsEqual(list_args,
                          ["test1", "test2", r"test1\aa", r"test2\aa"])

    list_args = collect_flow.InterpolateList(["one"])
    self.assertEqual(list_args, ["one"])

    # Ignore the failure in users.desktop, report the others.
    collect_flow.args.ignore_interpolation_errors = True
    list_args = collect_flow.InterpolateList(
        ["%%users.desktop%%", r"%%users.username%%\aa"])
    self.assertItemsEqual(list_args, [r"test1\aa", r"test2\aa"])

    # Both fail.
    list_args = collect_flow.InterpolateList(
        [r"%%users.desktop%%\aa", r"%%users.sid%%\aa"])
    self.assertItemsEqual(list_args, [])

  def testGrepRegexCombination(self):
    collect_flow = collectors.ArtifactCollectorFlow(None, token=self.token)
    self.assertEqual(collect_flow._CombineRegex([r"simple"]), "simple")
    self.assertEqual(collect_flow._CombineRegex(["a", "b"]), "(a)|(b)")
    self.assertEqual(collect_flow._CombineRegex(["a", "b", "c"]), "(a)|(b)|(c)")
    self.assertEqual(
        collect_flow._CombineRegex(["a|b", "[^_]b", "c|d"]),
        "(a|b)|([^_]b)|(c|d)")

  def testGrep(self):

    class MockCallFlow(object):

      def CallFlow(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    mock_call_flow = MockCallFlow()
    with utils.Stubber(collectors.ArtifactCollectorFlow, "CallFlow",
                       mock_call_flow.CallFlow):

      collect_flow = collectors.ArtifactCollectorFlow(None, token=self.token)
      collect_flow.args = mock.Mock()
      collect_flow.args.ignore_interpolation_errors = False
      kb = rdf_client.KnowledgeBase()
      kb.MergeOrAddUser(rdf_client.User(username="test1"))
      kb.MergeOrAddUser(rdf_client.User(username="test2"))
      collect_flow.state["knowledge_base"] = kb
      collect_flow.current_artifact_name = "blah"

      collector = rdf_artifacts.ArtifactSource(
          type=rdf_artifacts.ArtifactSource.SourceType.GREP,
          attributes={
              "paths": ["/etc/passwd"],
              "content_regex_list": [r"^a%%users.username%%b$"]
          })
      collect_flow.Grep(collector, rdf_paths.PathSpec.PathType.TSK)

    conditions = mock_call_flow.kwargs["conditions"]
    self.assertEqual(len(conditions), 1)
    regexes = conditions[0].contents_regex_match.regex.SerializeToString()
    self.assertItemsEqual(regexes.split("|"), ["(^atest1b$)", "(^atest2b$)"])
    self.assertEqual(mock_call_flow.kwargs["paths"], ["/etc/passwd"])

  def testGetArtifact1(self):
    """Test we can get a basic artifact."""

    client_mock = action_mocks.FileFinderClientMock()
    client = aff4.FACTORY.Open(self.client_id, token=self.token, mode="rw")
    client.Set(client.Schema.SYSTEM("Linux"))
    client.Flush()

    # Dynamically add an ArtifactSource specifying the base path.
    file_path = os.path.join(self.base_path, "test_img.dd")
    coll1 = rdf_artifacts.ArtifactSource(
        type=rdf_artifacts.ArtifactSource.SourceType.FILE,
        attributes={"paths": [file_path]})
    self.fakeartifact.sources.append(coll1)

    artifact_list = ["FakeArtifact"]
    flow_test_lib.TestFlowHelper(
        collectors.ArtifactCollectorFlow.__name__,
        client_mock,
        artifact_list=artifact_list,
        use_tsk=False,
        token=self.token,
        client_id=self.client_id)

    # Test the AFF4 file that was created.
    fd1 = aff4.FACTORY.Open(
        "%s/fs/os/%s" % (self.client_id, file_path), token=self.token)
    fd2 = open(file_path, "rb")
    fd2.seek(0, 2)

    self.assertEqual(fd2.tell(), int(fd1.Get(fd1.Schema.SIZE)))

  def testArtifactSkipping(self):
    client_mock = action_mocks.ActionMock()
    client = aff4.FACTORY.Open(self.client_id, token=self.token, mode="rw")
    # This does not match the Artifact so it will not be collected.
    client.Set(client.Schema.SYSTEM("Windows"))
    kb = client.Get(client.Schema.KNOWLEDGE_BASE)
    kb.os = "Windows"
    client.Set(client.Schema.KNOWLEDGE_BASE, kb)
    client.Flush()

    artifact_list = ["FakeArtifact"]
    session_id = flow_test_lib.TestFlowHelper(
        collectors.ArtifactCollectorFlow.__name__,
        client_mock,
        artifact_list=artifact_list,
        use_tsk=False,
        token=self.token,
        client_id=self.client_id)

    flow_obj = aff4.FACTORY.Open(session_id, token=self.token)
    self.assertEqual(len(flow_obj.state.artifacts_skipped_due_to_condition), 1)
    self.assertEqual(flow_obj.state.artifacts_skipped_due_to_condition[0],
                     ["FakeArtifact", "os == 'Linux'"])

  def testRunGrrClientActionArtifact(self):
    """Test we can get a GRR client artifact."""
    with utils.Stubber(psutil, "process_iter", ProcessIter):
      client_mock = action_mocks.ActionMock(standard.ListProcesses)
      client = aff4.FACTORY.Open(self.client_id, token=self.token, mode="rw")
      client.Set(client.Schema.SYSTEM("Linux"))
      client.Flush()

      coll1 = rdf_artifacts.ArtifactSource(
          type=rdf_artifacts.ArtifactSource.SourceType.GRR_CLIENT_ACTION,
          attributes={"client_action": standard.ListProcesses.__name__})
      self.fakeartifact.sources.append(coll1)
      artifact_list = ["FakeArtifact"]
      session_id = flow_test_lib.TestFlowHelper(
          collectors.ArtifactCollectorFlow.__name__,
          client_mock,
          artifact_list=artifact_list,
          token=self.token,
          client_id=self.client_id)

      fd = flow.GRRFlow.ResultCollectionForFID(session_id)
      self.assertTrue(isinstance(list(fd)[0], rdf_client.Process))
      self.assertTrue(len(fd) == 1)

  def testRunGrrClientActionArtifactSplit(self):
    """Test that artifacts get split into separate collections."""
    with utils.Stubber(psutil, "process_iter", ProcessIter):
      client_mock = action_mocks.ActionMock(standard.ListProcesses)
      client = aff4.FACTORY.Open(self.client_id, token=self.token, mode="rw")
      client.Set(client.Schema.SYSTEM("Linux"))
      client.Flush()

      coll1 = rdf_artifacts.ArtifactSource(
          type=rdf_artifacts.ArtifactSource.SourceType.GRR_CLIENT_ACTION,
          attributes={"client_action": standard.ListProcesses.__name__})
      self.fakeartifact.sources.append(coll1)
      self.fakeartifact2.sources.append(coll1)
      artifact_list = ["FakeArtifact", "FakeArtifact2"]
      session_id = flow_test_lib.TestFlowHelper(
          collectors.ArtifactCollectorFlow.__name__,
          client_mock,
          artifact_list=artifact_list,
          token=self.token,
          client_id=self.client_id,
          split_output_by_artifact=True)

      # Check that we got two separate collections based on artifact name
      fd = collectors.ArtifactCollectorFlow.ResultCollectionForArtifact(
          session_id, "FakeArtifact")

      self.assertTrue(isinstance(list(fd)[0], rdf_client.Process))
      self.assertEqual(len(fd), 1)

      fd = collectors.ArtifactCollectorFlow.ResultCollectionForArtifact(
          session_id, "FakeArtifact2")
      self.assertEqual(len(fd), 1)
      self.assertTrue(isinstance(list(fd)[0], rdf_client.Process))

  def testConditions(self):
    """Test we can get a GRR client artifact with conditions."""
    with utils.Stubber(psutil, "process_iter", ProcessIter):
      # Run with false condition.
      client_mock = action_mocks.ActionMock(standard.ListProcesses)
      coll1 = rdf_artifacts.ArtifactSource(
          type=rdf_artifacts.ArtifactSource.SourceType.GRR_CLIENT_ACTION,
          attributes={"client_action": standard.ListProcesses.__name__},
          conditions=["os == 'Windows'"])
      self.fakeartifact.sources.append(coll1)
      fd = self._RunClientActionArtifact(client_mock, ["FakeArtifact"])
      self.assertEqual(fd.__class__,
                       sequential_collection.GeneralIndexedCollection)
      self.assertEqual(len(fd), 0)

      # Now run with matching or condition.
      coll1.conditions = ["os == 'Linux' or os == 'Windows'"]
      self.fakeartifact.sources = []
      self.fakeartifact.sources.append(coll1)
      fd = self._RunClientActionArtifact(client_mock, ["FakeArtifact"])
      self.assertEqual(fd.__class__,
                       sequential_collection.GeneralIndexedCollection)
      self.assertNotEqual(len(fd), 0)

      # Now run with impossible or condition.
      coll1.conditions.append("os == 'NotTrue'")
      self.fakeartifact.sources = []
      self.fakeartifact.sources.append(coll1)
      fd = self._RunClientActionArtifact(client_mock, ["FakeArtifact"])
      self.assertEqual(fd.__class__,
                       sequential_collection.GeneralIndexedCollection)
      self.assertEqual(len(fd), 0)

  def testRegistryValueArtifact(self):
    with vfs_test_lib.VFSOverrider(rdf_paths.PathSpec.PathType.REGISTRY,
                                   vfs_test_lib.FakeRegistryVFSHandler):
      with vfs_test_lib.VFSOverrider(rdf_paths.PathSpec.PathType.OS,
                                     vfs_test_lib.FakeFullVFSHandler):

        client_mock = action_mocks.ActionMock(standard.GetFileStat)
        coll1 = rdf_artifacts.ArtifactSource(
            type=rdf_artifacts.ArtifactSource.SourceType.REGISTRY_VALUE,
            attributes={
                "key_value_pairs": [{
                    "key": (r"HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet"
                            r"\Control\Session Manager"),
                    "value":
                        "BootExecute"
                }]
            })
        self.fakeartifact.sources.append(coll1)
        artifact_list = ["FakeArtifact"]
        session_id = flow_test_lib.TestFlowHelper(
            collectors.ArtifactCollectorFlow.__name__,
            client_mock,
            artifact_list=artifact_list,
            token=self.token,
            client_id=self.client_id)

    # Test the statentry got stored.
    fd = flow.GRRFlow.ResultCollectionForFID(session_id)
    self.assertTrue(isinstance(list(fd)[0], rdf_client.StatEntry))
    urn = fd[0].pathspec.AFF4Path(self.client_id)
    self.assertTrue(str(urn).endswith("BootExecute"))

  def testRegistryDefaultValueArtifact(self):
    with vfs_test_lib.VFSOverrider(rdf_paths.PathSpec.PathType.REGISTRY,
                                   vfs_test_lib.FakeRegistryVFSHandler):
      with vfs_test_lib.VFSOverrider(rdf_paths.PathSpec.PathType.OS,
                                     vfs_test_lib.FakeFullVFSHandler):

        client_mock = action_mocks.ActionMock(standard.GetFileStat)
        coll1 = rdf_artifacts.ArtifactSource(
            type=rdf_artifacts.ArtifactSource.SourceType.REGISTRY_VALUE,
            attributes={
                "key_value_pairs": [{
                    "key": (r"HKEY_LOCAL_MACHINE/SOFTWARE/ListingTest"),
                    "value": ""
                }]
            })
        self.fakeartifact.sources.append(coll1)
        artifact_list = ["FakeArtifact"]
        session_id = flow_test_lib.TestFlowHelper(
            collectors.ArtifactCollectorFlow.__name__,
            client_mock,
            artifact_list=artifact_list,
            token=self.token,
            client_id=self.client_id)

    fd = flow.GRRFlow.ResultCollectionForFID(session_id)
    self.assertTrue(isinstance(list(fd)[0], rdf_client.StatEntry))
    self.assertEqual(fd[0].registry_data.GetValue(), "DefaultValue")

  def testSupportedOS(self):
    """Test supported_os inside the collector object."""
    with utils.Stubber(psutil, "process_iter", ProcessIter):
      # Run with false condition.
      client_mock = action_mocks.ActionMock(standard.ListProcesses)
      coll1 = rdf_artifacts.ArtifactSource(
          type=rdf_artifacts.ArtifactSource.SourceType.GRR_CLIENT_ACTION,
          attributes={"client_action": standard.ListProcesses.__name__},
          supported_os=["Windows"])
      self.fakeartifact.sources.append(coll1)
      fd = self._RunClientActionArtifact(client_mock, ["FakeArtifact"])
      self.assertEqual(fd.__class__,
                       sequential_collection.GeneralIndexedCollection)
      self.assertEqual(len(fd), 0)

      # Now run with matching or condition.
      coll1.conditions = []
      coll1.supported_os = ["Linux", "Windows"]
      self.fakeartifact.sources = []
      self.fakeartifact.sources.append(coll1)
      fd = self._RunClientActionArtifact(client_mock, ["FakeArtifact"])
      self.assertEqual(fd.__class__,
                       sequential_collection.GeneralIndexedCollection)
      self.assertNotEqual(len(fd), 0)

      # Now run with impossible or condition.
      coll1.conditions = ["os == 'Linux' or os == 'Windows'"]
      coll1.supported_os = ["NotTrue"]
      self.fakeartifact.sources = []
      self.fakeartifact.sources.append(coll1)
      fd = self._RunClientActionArtifact(client_mock, ["FakeArtifact"])
      self.assertEqual(fd.__class__,
                       sequential_collection.GeneralIndexedCollection)
      self.assertEqual(len(fd), 0)

  def _RunClientActionArtifact(self, client_mock, artifact_list):
    client = aff4.FACTORY.Open(self.client_id, token=self.token, mode="rw")
    client.Set(client.Schema.SYSTEM("Linux"))
    client.Flush()
    self.output_count += 1
    session_id = flow_test_lib.TestFlowHelper(
        collectors.ArtifactCollectorFlow.__name__,
        client_mock,
        artifact_list=artifact_list,
        token=self.token,
        client_id=self.client_id)

    return flow.GRRFlow.ResultCollectionForFID(session_id)


class TestClientArtifactCollector(flow_test_lib.FlowTestsBaseclass):
  """Test the clientside artifact collection test artifacts."""

  def setUp(self):
    super(TestClientArtifactCollector, self).setUp()
    test_artifacts_file = os.path.join(config.CONFIG["Test.data_dir"],
                                       "artifacts", "test_artifacts.json")
    artifact_registry.REGISTRY.AddFileSource(test_artifacts_file)

    self.client_id = self.SetupClient(0)

    with aff4.FACTORY.Open(self.client_id, token=self.token, mode="rw") as fd:
      fd.Set(fd.Schema.SYSTEM("Linux"))
      kb = fd.Schema.KNOWLEDGE_BASE()
      artifact.SetCoreGRRKnowledgeBaseValues(kb, fd)
      fd.Set(kb)

  def testKnowledgeBase(self):
    """Test that the knowledge base is passed in the bundle."""
    artifact_collector = collectors.ClientArtifactCollector(None)
    artifact_collector.args = artifact_utils.ArtifactCollectorFlowArgs()

    kb = rdf_client.KnowledgeBase()
    kb.os = "Windows"
    artifact_collector.args.knowledge_base = kb

    artifact_bundle = artifact_collector._GetArtifactCollectorArgs([])

    self.assertEqual(artifact_bundle.knowledge_base.os, "Windows")

  def testPrepareBasicArtifactBundle(self):
    """Test we can prepare a basic artifact."""
    artifact_list = ["TestCmdArtifact"]
    artifact_collector = collectors.ClientArtifactCollector(None)
    artifact_collector.args = artifact_utils.ArtifactCollectorFlowArgs()

    artifact_bundle = artifact_collector._GetArtifactCollectorArgs(
        artifact_list)
    artifacts_objects = list(artifact_bundle.artifacts)

    art_obj = artifacts_objects[0]
    source = list(art_obj.sources)[0]

    self.assertEqual(art_obj.name, "TestCmdArtifact")
    self.assertEqual(source.base_source.attributes["cmd"], "/usr/bin/dpkg")
    self.assertEqual(source.base_source.attributes.get("args", []), ["--list"])

  def testSourceMeetsConditions(self):
    """Test we can get a GRR client artifact with conditions."""
    artifact_collector = collectors.ClientArtifactCollector(None)
    artifact_collector.args = artifact_utils.ArtifactCollectorFlowArgs()

    kb = rdf_client.KnowledgeBase()
    kb.os = "Windows"
    artifact_collector.args.knowledge_base = kb

    # Run with false condition.
    source = rdf_artifacts.ArtifactSource(
        type=rdf_artifacts.ArtifactSource.SourceType.GRR_CLIENT_ACTION,
        attributes={"client_action": standard.ListProcesses.__name__},
        conditions=["os == 'Linux'"])
    self.assertFalse(artifact_collector._MeetsConditions(source))

    # Run with matching or condition.
    source = rdf_artifacts.ArtifactSource(
        type=rdf_artifacts.ArtifactSource.SourceType.GRR_CLIENT_ACTION,
        attributes={"client_action": standard.ListProcesses.__name__},
        conditions=["os == 'Linux' or os == 'Windows'"])
    self.assertTrue(artifact_collector._MeetsConditions(source))

  def testPrepareAggregatedArtifactBundle(self):
    """Test we can prepare the source artifacts of an aggregation artifact."""
    artifact_list = ["TestAggregationArtifact"]
    artifact_collector = collectors.ClientArtifactCollector(None)
    artifact_collector.args = artifact_utils.ArtifactCollectorFlowArgs()

    artifact_bundle = artifact_collector._GetArtifactCollectorArgs(
        artifact_list)
    artifacts_objects = list(artifact_bundle.artifacts)

    art_obj = artifacts_objects[0]
    self.assertEqual(art_obj.name, "TestAggregationArtifact")

    source = list(art_obj.sources)[0]
    self.assertEqual(source.base_source.type, "GRR_CLIENT_ACTION")

    source = list(art_obj.sources)[1]
    self.assertEqual(source.base_source.type, "COMMAND")

  def testPrepareMultipleArtifacts(self):
    """Test we can prepare multiple artifacts of different types."""
    artifact_list = [
        "TestFilesArtifact", "DepsWindirRegex", "DepsProvidesMultiple",
        "WMIActiveScriptEventConsumer"
    ]
    artifact_collector = collectors.ClientArtifactCollector(None)
    artifact_collector.args = artifact_utils.ArtifactCollectorFlowArgs()

    artifact_bundle = artifact_collector._GetArtifactCollectorArgs(
        artifact_list)
    artifacts_objects = list(artifact_bundle.artifacts)

    self.assertEqual(len(artifacts_objects), 4)
    self.assertEqual(artifacts_objects[0].name, "TestFilesArtifact")
    self.assertEqual(artifacts_objects[1].name, "DepsWindirRegex")
    self.assertEqual(artifacts_objects[2].name, "DepsProvidesMultiple")
    self.assertEqual(artifacts_objects[3].name, "WMIActiveScriptEventConsumer")

    art_obj = artifacts_objects[3]
    source = list(art_obj.sources)[0]
    self.assertEqual(source.base_source.attributes["query"],
                     "SELECT * FROM ActiveScriptEventConsumer")

  def testDuplicationChecks(self):
    """Test duplicated artifacts are only processed once."""
    artifact_list = [
        "TestAggregationArtifact", "TestFilesArtifact", "TestCmdArtifact",
        "TestFilesArtifact"
    ]
    artifact_collector = collectors.ClientArtifactCollector(None)
    artifact_collector.args = artifact_utils.ArtifactCollectorFlowArgs()

    artifact_bundle = artifact_collector._GetArtifactCollectorArgs(
        artifact_list)
    artifacts_objects = list(artifact_bundle.artifacts)

    self.assertEqual(len(artifacts_objects), 2)


def main(argv):
  # Run the full test suite
  test_lib.main(argv)


if __name__ == "__main__":
  flags.StartMain(main)
