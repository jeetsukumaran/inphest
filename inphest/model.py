#! /usr/bin/env python

try:
    from StringIO import StringIO # Python 2 legacy support: StringIO in this module is the one needed (not io)
except ImportError:
    from io import StringIO # Python 3
import sys
import os
import random
import collections
import argparse
import pprint
import copy
import json
from distutils.util import strtobool
import dendropy

from inphest import utility
from inphest import revbayes
from inphest import error

def weighted_choice(seq, weights, rng):
    """
    Selects an element out of seq, with probabilities of each element
    given by the list `weights` (which must be at least as long as the
    length of `seq` - 1).
    """
    if weights is None:
        weights = [1.0/len(seq) for count in range(len(seq))]
    else:
        weights = list(weights)
    if len(weights) < len(seq) - 1:
        raise Exception("Insufficient number of weights specified")
    sow = sum(weights)
    if len(weights) == len(seq) - 1:
        weights.append(1 - sow)
    return seq[weighted_index_choice(weights, sow, rng)]

def weighted_index_choice(weights, sum_of_weights, rng):
    """
    (From: http://eli.thegreenplace.net/2010/01/22/weighted-random-generation-in-python/)
    The following is a simple function to implement weighted random choice in
    Python. Given a list of weights, it returns an index randomly, according
    to these weights [1].
    For example, given [2, 3, 5] it returns 0 (the index of the first element)
    with probability 0.2, 1 with probability 0.3 and 2 with probability 0.5.
    The weights need not sum up to anything in particular, and can actually be
    arbitrary Python floating point numbers.
    If we manage to sort the weights in descending order before passing them
    to weighted_choice_sub, it will run even faster, since the random call
    returns a uniformly distributed value and larger chunks of the total
    weight will be skipped in the beginning.
    """
    rnd = rng.uniform(0, 1) * sum_of_weights
    for i, w in enumerate(weights):
        rnd -= w
        if rnd < 0:
            return i

class StatesVector(object):
    """
    A vector in which each element is an integer represents the state of a
    trait.

    E.g.,

        [1,0,1,2]

    is a 4-trait vector, where trait 0 is in state 1, trait 1 is in
    state 0, and so on.
    """

    def __init__(self,
            nchar,
            nstates=None,
            values=None,
            ):
        """
        Parameters
        ----------
        nchar : integer
            The number of traits to be tracked.
        nstates : list of integers
            The number of states for each trait. If not specified, defaults
            to binary (i.e., 2 states, 0 and 1). If specifed, must be a list of
            length `nchar`, with each element in the list being integer > 0.
        values : iterable of ints
            Vector of initial values. If not specified, defaults to all 0's.
        """
        self._nchar = nchar
        if nstates is not None:
            self._nstates = list(nstates)
        else:
            self._nstates = [2] * nchar
        if not values:
            self._states = [0] * nchar
        else:
            assert len(values) == nchar
            self._states = list(values)

    def clone(self):
        s = self.__class__(
                nchar=self._nchar,
                nstates=self._nstates,
            )
        s._states = list(self._states)
        return s

    @property
    def nchar(self):
        return len(self)

    def __len__(self):
        return self._nchar

    def __getitem__(self, idx):
        return self._states[idx]

    def __setitem__(self, idx, v):
        self._states[idx] = v

    def __repr__(self):
        return str(self._states)

class RateFunction(object):

    @classmethod
    def from_definition_dict(cls, rate_function_d):
        rf = cls()
        rf.parse_definition(rate_function_d)
        return rf

    def __init__(self,
            definition_type=None,
            definition_content=None,
            description=None,
            ):
        self.definition_type = definition_type # value, lambda, function, map
        self.definition_content = definition_content
        self.description = description
        self._compute_rate = None
        if self.definition_content is not None:
            self.compile_function()

    def __call__(self, **kwargs):
        return self._compute_rate(**kwargs)

    def parse_definition(self, rate_function_d):
        rate_function_d = dict(rate_function_d)
        self.definition_type = rate_function_d.pop("definition_type").replace("-", "_")
        self.definition_content = rate_function_d.pop("definition")
        self.description = rate_function_d.pop("description", "")
        if rate_function_d:
            raise TypeError("Unsupported function definition keywords: {}".format(rate_function_d))
        self.compile_function()

    def compile_function(self):
        self.definition_type = self.definition_type.replace("-", "_")
        if self.definition_type == "fixed_value":
            self.definition_content = float(self.definition_content)
            self._compute_rate = lambda **kwargs: self.definition_content
        elif self.definition_type == "lambda_definition":
            self._compute_rate = eval(self.definition_content)
        elif self.definition_type == "function_object":
            self._compute_rate = self.definition_content
        else:
            raise ValueError("Unrecognized function definition type: '{}'".format(self.definition_type))

    def as_definition(self):
        d = collections.OrderedDict()
        d["definition_type"] = self.definition_type
        if d["definition_type"] == "function_object":
            d["definition"] = str(self.definition_content)
        else:
            d["definition"] = self.definition_content
        d["description"] = self.description
        return d

class HostHistory(object):
    """
    A particular host history on which the symbiont history is conditioned.
    """

    HostLineageDefinition = collections.namedtuple("HostLineageDefinition", [
        # "tree_idx",                 #   identifer of tree from which this lineage has been sampled (same lineage, as given by split id will occur on different trees/histories)
        "lineage_id",               #   lineage (edge/split) id on which event occurs
        "lineage_parent_id",
        "leafset_bitstring",
        "split_bitstring",
        # "rb_index",
        "lineage_start_time",          # time lineage appears
        "lineage_end_time",            # time lineage ends
        "lineage_start_distribution_bitstring",  #   distribution/range (area set) at beginning of lineage
        "lineage_end_distribution_bitstring",    #   distribution/range (area set) at end of lineage
        "is_seed_node",
        "is_leaf",
        "is_extant_leaf",
    ])

    HostEvent = collections.namedtuple("HostEvent", [
        # "tree_idx",                 #   identifer of tree from which this event has been sampled
        "event_time",               #   time of event
        "weight",                   #   probability of event (1.0 if we take history as truth)
        "lineage_id",               #   lineage (edge/split) id on which event occurs
        "event_type",               #   type of event: anagenesis, cladogenesis
        "event_subtype",            #   if anagenesis: area_loss, area_gain; if cladogenesis: narrow sympatry etc.
        "area_idx",                 #   area involved in event (anagenetic)
        "child0_lineage_id",        #   split/edge id of first daughter (cladogenesis)
        "child1_lineage_id",        #   split/edge id of second daughter (cladogenesis)
        ])

    def __init__(self, taxon_namespace=None,):
        if taxon_namespace is None:
            self.taxon_namespace = dendropy.TaxonNamespace()
        else:
            self.taxon_namespace = taxon_namespace
        self.events = [] # collection of HostEvent objects, sorted by time
        self.lineages = {} # keys: lineage_id (== int(Bipartition) == Bipartition.split_bitmask); values: HostLineageDefinition
        self.start_time = None
        self.end_time = None

    def compile(self, tree, start_time, end_time):
        self.tree = tree
        ndm = self.tree.node_distance_matrix()
        self.lineage_distance_matrix = {}
        self.area_assemblage_leaf_sets = None
        self.extant_leaf_nodes = set()
        for node1 in ndm:
            key1 = int(node1.edge.bipartition.split_bitmask)
            assert key1 in self.lineages, key1
            if key1 not in self.lineage_distance_matrix:
                self.lineage_distance_matrix[key1] = {}
                node1.lineage_definition = self.lineages[key1]
                if node1.lineage_definition.is_extant_leaf:
                    node1.taxon.is_extant_leaf = True
                    self.extant_leaf_nodes.add(node1)
                    for idx, presence in enumerate(node1.lineage_definition.lineage_end_distribution_bitstring):
                        if self.area_assemblage_leaf_sets is None:
                            self.area_assemblage_leaf_sets = [set() for i in range(len(node1.lineage_definition.lineage_end_distribution_bitstring))]
                        else:
                            assert len(self.area_assemblage_leaf_sets) == len(node1.lineage_definition.lineage_end_distribution_bitstring)
                        if presence == "1":
                            self.area_assemblage_leaf_sets[idx].add(node1)
                        else:
                            assert presence == "0"
                elif node1.taxon is not None:
                    node1.taxon.is_extant_leaf = False
            for node2 in ndm:
                key2 = int(node2.edge.bipartition.split_bitmask)
                assert key2 in self.lineages
                self.lineage_distance_matrix[key1][key2] = ndm.patristic_distance(node1, node2, is_normalize_by_tree_size=True)
        self.start_time = start_time
        self.end_time = end_time
        self.events.sort(key=lambda x: x.event_time, reverse=False)
        for event in self.events:
            assert event.event_time >= self.start_time
            assert event.event_time <= self.end_time, "{} > {}".format(event.event_time, self.end_time)

    def validate(self):
        ## Basically, runs through the histories for each edge/lineage,
        ## ensuring the history correctly reproduces the end state given the start state
        lineage_events = collections.defaultdict(list)
        for event in self.events:
            lineage_events[event.lineage_id].append(event)
        for lineage_id in lineage_events:
            distribution_bitlist = list(self.lineages[lineage_id].lineage_start_distribution_bitstring)
            lineage_events[event.lineage_id].sort(key=lambda x: x.event_time, reverse=False)
            for event in lineage_events[lineage_id]:
                assert utility.is_in_range(event.event_time, self.lineages[lineage_id].lineage_start_time, self.lineages[lineage_id].lineage_end_time,), "{}: {} <= {} <= {}: False".format(lineage_id, self.lineages[lineage_id].lineage_start_time, event.event_time, self.lineages[lineage_id].lineage_end_time)
                # assert event.event_time >= self.lineages[lineage_id].lineage_start_time, "{}: {} >= {}: False".format(lineage_id, event.event_time, self.lineages[lineage_id].lineage_start_time)
                # assert event.event_time <= self.lineages[lineage_id].lineage_end_time, "{}: {} <= {}: False".format(lineage_id, event.event_time, self.lineages[lineage_id].lineage_end_time)
                if event.event_type == "anagenesis" and event.event_subtype == "area_gain":
                    assert distribution_bitlist[event.area_idx] == "0", "Lineage {} at time {}: Trying to add area with index {} to distribution that already has area: {}".format(
                            lineage_id,
                            event.event_time,
                            event.area_idx,
                            "".join(distribution_bitlist))
                    distribution_bitlist[event.area_idx] == "1"
                elif event.event_type == "anagenesis" and event.event_subtype == "area_loss":
                    assert distribution_bitlist[event.area_idx] == "1", "Lineage {} at time {}: Trying to remove area with index {} from distribution that does not have area: {}".format(
                            lineage_id,
                            event.event_time,
                            event.area_idx,
                            "".join(distribution_bitlist))
                    distribution_bitlist[event.area_idx] == "0"
                elif event.event_type == "cladogenesis":
                    assert "".join(distribution_bitlist) == self.lineages[lineage_id].lineage_start_distribution_bitstring

    def generate_areas(self):
        num_areas = None
        for host_history_lineage_id_definition in self.lineages.values():
            if num_areas is None:
                num_areas = len(host_history_lineage_id_definition.lineage_start_distribution_bitstring)
            assert num_areas == len(host_history_lineage_id_definition.lineage_start_distribution_bitstring)
            assert num_areas == len(host_history_lineage_id_definition.lineage_end_distribution_bitstring),  "{}: {}".format(num_areas, host_history_lineage_id_definition.lineage_end_distribution_bitstring)
        areas = []
        for area_idx in range(num_areas):
            area = Area(area_idx)
            areas.append(area)
        return areas

class HostHistorySamples(object):
    """
    A collection of host histories, one a single one of each a particular symbiont history will be conditioned.
    """

    def __init__(self):
        self.host_histories = []

    def parse_host_biogeography(self,
            src,
            schema,
            validate=True,
            ignore_validation_errors=False):
        if schema == "revbayes":
            self.parse_rb_host_biogeography(src=src,
                    validate=validate,
                    ignore_validation_errors=ignore_validation_errors)
        else:
            self.parse_archipelago_host_biogeography(src=src,
                    validate=validate,
                    ignore_validation_errors=ignore_validation_errors)

    def parse_archipelago_host_biogeography(self,
            src,
            validate=True,
            ignore_validation_errors=False):
        data = json.load(src)
        for history_sample in data:
            taxon_namespace = dendropy.TaxonNamespace(history_sample["leaf_labels"])
            host_history = HostHistory(taxon_namespace=taxon_namespace)
            for lineage_d in history_sample["lineages"]:
                lineage = HostHistory.HostLineageDefinition(
                        lineage_id=lineage_d["lineage_id"],
                        lineage_parent_id=lineage_d["lineage_parent_id"],
                        leafset_bitstring=lineage_d["leafset_bitstring"],
                        split_bitstring=lineage_d["split_bitstring"],
                        lineage_start_time=lineage_d["lineage_start_time"],
                        lineage_end_time=lineage_d["lineage_end_time"],
                        lineage_start_distribution_bitstring=lineage_d["lineage_start_distribution_bitstring"],
                        lineage_end_distribution_bitstring=lineage_d["lineage_end_distribution_bitstring"],
                        is_seed_node=lineage_d["is_seed_node"],
                        is_leaf=lineage_d["is_leaf"],
                        is_extant_leaf=lineage_d["is_extant_leaf"],
                        )
                assert lineage.lineage_id not in host_history.lineages
                assert lineage.lineage_start_time <= lineage.lineage_end_time, "{}, {}".format(lineage.lineage_start_time, lineage.lineage_end_time)
                host_history.lineages[lineage.lineage_id] = lineage
            for event_d in history_sample["events"]:
                # if event_d["event_type"] == "extinction":
                #     continue
                if event_d["event_type"] == "trait_evolution":
                    continue
                event = HostHistory.HostEvent(
                    event_time=event_d["event_time"],
                    weight=1.0,
                    lineage_id=event_d["lineage_id"],
                    event_type=event_d["event_type"],
                    event_subtype=event_d["event_subtype"],
                    area_idx=event_d.get("state_idx", None),
                    child0_lineage_id=event_d.get("child0_lineage_id", None),
                    child1_lineage_id=event_d.get("child1_lineage_id", None),
                    )
                assert event.lineage_id in host_history.lineages
                host_history.events.append(event)
            host_tree = dendropy.Tree.get(
                    data=history_sample["tree"]["newick"],
                    schema="newick",
                    rooting="force-rooted",
                    taxon_namespace=taxon_namespace,
                    )
            host_tree.encode_bipartitions()
            # host_tree = None
            host_history.compile(
                tree=host_tree,
                start_time=0.0,
                end_time=history_sample["tree"]["end_time"],
                )
            if validate:
                host_history.validate()
            self.host_histories.append(host_history)

    def parse_rb_host_biogeography(self,
            src,
            validate=True,
            ignore_validation_errors=False):
        """
        Reads the output of RevBayes biogeographical history.
        """
        rb = revbayes.RevBayesBiogeographyParser(taxon_namespace=self.taxon_namespace)
        rb.parse(src)

        # total_tree_ln_likelihoods = 0.0
        # for tree_entry in rb.tree_entries:
        #     total_tree_ln_likelihoods += tree_entry["posterior"]
        # for tree_entry in rb.tree_entries:
        #     self.tree_probabilities.append(tree_entry["posterior"]/total_tree_ln_likelihoods)

        tree_host_histories = {}
        # tree_root_heights = {}
        for edge_entry in rb.edge_entries:
            tree_idx = edge_entry["tree_idx"]
            if tree_idx not in tree_host_histories:
                tree_host_histories[tree_idx] = HostHistory(taxon_namespace=self.taxon_namespace)
            lineage_id = edge_entry["edge_id"]
            lineage = HostHistory.HostLineageDefinition(
                    # tree_idx=edge_entry["tree_idx"],
                    lineage_id=lineage_id,
                    lineage_parent_id=edge_entry["parent_edge_id"],
                    leafset_bitstring=edge_entry["leafset_bitstring"],
                    split_bitstring=edge_entry["split_bitstring"],
                    # rb_index=edge_entry["rb_index"],
                    lineage_start_time=edge_entry["edge_start_time"],
                    lineage_end_time=edge_entry["edge_end_time"],
                    lineage_start_distribution_bitstring=edge_entry["edge_starting_state"],
                    lineage_end_distribution_bitstring=edge_entry["edge_ending_state"],
                    is_seed_node=edge_entry["is_seed_node"],
                    is_leaf=edge_entry["is_leaf"],
                    is_extant_leaf=edge_entry["is_leaf"],
                    )
            assert lineage.lineage_id not in tree_host_histories[tree_idx].lineages
            assert lineage.lineage_start_time <= lineage.lineage_end_time, "{}, {}".format(lineage.lineage_start_time, lineage.lineage_end_time)
            tree_host_histories[tree_idx].lineages[lineage_id] = lineage
            # try:
            #     tree_root_heights[tree_idx] = max(edge_entry["edge_ending_age"], tree_root_heights[tree_idx])
            # except KeyError:
            #     tree_root_heights[tree_idx] = edge_entry["edge_ending_age"]

        for event_entry in rb.event_schedules_across_all_trees:
            tree_idx = event_entry["tree_idx"]
            if tree_idx not in tree_host_histories:
                tree_host_histories[tree_idx] = HostHistory(taxon_namespace=self.taxon_namespace)
            event = HostHistory.HostEvent(
                # tree_idx=event_entry["tree_idx"],
                event_time=event_entry["time"],
                # weight=self.tree_probabilities[event_entry["tree_idx"]],
                weight=1.0,
                lineage_id=event_entry["edge_id"],
                event_type=event_entry["event_type"],
                event_subtype=event_entry["event_subtype"],
                area_idx=event_entry.get("area_idx", None),
                child0_lineage_id=event_entry.get("child0_edge_id", None),
                child1_lineage_id=event_entry.get("child1_edge_id", None),
                )
            assert event.lineage_id in tree_host_histories[tree_idx].lineages
            tree_host_histories[tree_idx].events.append(event)

        for tree_idx in tree_host_histories:
            host_history = tree_host_histories[tree_idx]
            end_time = max(rb.tree_entries[tree_idx]["seed_node_age"], rb.max_event_times[tree_idx])
            host_history.compile(
                    tree=rb.tree_entries[tree_idx]["tree"],
                    start_time=0.0,
                    end_time=end_time,
                    )
            if validate:
                host_history.validate()
            self.host_histories.append(host_history)

class Area(object):

    """
    Manages the state of an area during a particular simulation replicate.
    """

    def __init__(self, area_idx):
        self.area_idx = area_idx
        self.host_lineages = set()
        self.symbiont_lineages = set()

    def __str__(self):
        return "Area{}".format(self.area_idx)

    def __repr__(self):
        return "<inphest.model.Area object at {} with index {}>".format(id(self), self.area_idx)

class HostLineage(object):
    """
    Manages the state of a host during a particular simulation replicate.
    """

    def __init__(self,
            host_history_lineage_definition,
            host_system,
            debug_mode):
        self.host_history_lineage_definition = host_history_lineage_definition
        self.host_system = host_system
        self.host_to_symbiont_time_scale_factor = host_system.host_to_symbiont_time_scale_factor
        self.lineage_id = host_history_lineage_definition.lineage_id
        self.lineage_parent_id = host_history_lineage_definition.lineage_parent_id
        self.leafset_bitstring = host_history_lineage_definition.leafset_bitstring
        self.split_bitstring = host_history_lineage_definition.split_bitstring
        # self.rb_index = host_history_lineage_definition.rb_index
        self.start_time = host_history_lineage_definition.lineage_start_time * self.host_to_symbiont_time_scale_factor
        self.end_time = host_history_lineage_definition.lineage_end_time * self.host_to_symbiont_time_scale_factor
        self.start_distribution_bitstring = host_history_lineage_definition.lineage_start_distribution_bitstring
        self.end_distribution_bitstring = host_history_lineage_definition.lineage_end_distribution_bitstring
        self.is_seed_node = host_history_lineage_definition.is_seed_node
        self.is_leaf = host_history_lineage_definition.is_leaf
        self.is_extant_leaf = host_history_lineage_definition.is_extant_leaf
        self._current_areas = set()
        self.extancy = "pre"
        self.debug_mode = False

    def __str__(self):
        return str(self.lineage_id)

    def activate(self, simulation_elapsed_time=None, debug_mode=None):
        assert self.extancy == "pre"
        if debug_mode is not None:
            self.debug_mode = debug_mode
        if simulation_elapsed_time is not None:
            try:
                assert utility.is_in_range(simulation_elapsed_time, self.start_time, self.end_time,), "{}: {} <= {} <= {}: False".format(lineage_id, self.start_time, simulation_elapsed_time, self.end_time)
            except AssertionError:
                print("{}, start at {}, end at {}, current time = {}".format(self.lineage_id, self.start_time, self.end_time, simulation_elapsed_time))
                raise
        assert len(self.host_system.areas) == len(self.start_distribution_bitstring)
        for (area_idx, area), (d_idx, presence) in zip(enumerate(self.host_system.areas), enumerate(self.start_distribution_bitstring)):
            assert area_idx == d_idx
            assert area.area_idx == area_idx
            if presence == "1":
                self.add_area(area)
            else:
                assert presence == "0"
        if self.debug_mode:
            self._current_distribution_check_bitlist = list(self.start_distribution_bitstring)
        else:
            self._current_distribution_check_bitlist = None
        self.extancy = "current"
        self.is_post_area_gain = False

    def deactivate(self):
        assert self.extancy == "current"
        for area in self._current_areas:
            area.host_lineages.remove(self)
        self._current_areas = set()
        self.extancy = "post"

    def add_area(self, area):
        assert area not in self._current_areas
        self._current_areas.add(area)
        area.host_lineages.add(self)

    def remove_area(self, area):
        assert area in self._current_areas
        self._current_areas.remove(area)
        area.host_lineages.remove(self)

    def clear_areas(self):
        for area in self._current_areas:
            area.host_lineages.remove(self)
        self._current_areas.clear()

    def has_area(self, area):
        return area in self._current_areas

    def current_area_iter(self):
        for area in self._current_areas:
            yield area

    def debug_check(self, simulation_elapsed_time):
        if simulation_elapsed_time is not None:
            self.debug_check_extancy_state(simulation_elapsed_time)
        self.debug_check_distribution(simulation_elapsed_time=simulation_elapsed_time)

    def assert_correctly_extant(self, simulation_elapsed_time, ignore_fail=False):
        try:
            assert simulation_elapsed_time > self.start_time or utility.is_almost_equal(simulation_elapsed_time, self.start_time)
        except AssertionError:
            message = ("!! PREMATURELY ACTIVE HOST ERROR: {} ({}): times = {} to {}, current simulation elapsed time = {}".format(
                self.lineage_id,
                self.leafset_bitstring,
                self.start_time,
                self.end_time,
                simulation_elapsed_time))
            if not ignore_fail:
                raise AssertionError(message)
            else:
                print(message)
        try:
            assert simulation_elapsed_time < self.end_time or utility.is_almost_equal(simulation_elapsed_time, self.end_time)
        except AssertionError:
            message = ("!!  EXTINCT HOST ERROR: {} ({}): times = {} to {}, current simulation elapsed time = {}".format(
                    self.lineage_id,
                    self.leafset_bitstring,
                    self.start_time,
                    self.end_time,
                    simulation_elapsed_time))
            if not ignore_fail:
                raise AssertionError(message)
            else:
                print(message)
        try:
            assert self.extancy == "current"
        except AssertionError:
            message = "!! HOST {}: expecting extancy to be 'current', but instead found: '{}'".format(self.lineage_id, self.extancy)
            if not ignore_fail:
                raise AssertionError(message)
            else:
                print(message)

    def debug_check_extancy_state(self, simulation_elapsed_time):
        message = ("Lineage {} ({}): times = {} to {}, current simulation elapsed time = {}: designated as '{}'".format(
            self.lineage_id,
            self.leafset_bitstring,
            self.start_time,
            self.end_time,
            simulation_elapsed_time,
            self.extancy))
        # if simulation_elapsed_time < self.start_time:
        #     assert self.extancy == "pre", message
        # elif simulation_elapsed_time > self.end_time:
        #     assert self.extancy == "post", message
        # elif simulation_elapsed_time == self.start_time:
        #     assert self.extancy == "current" or self.extancy == "pre", message
        # elif simulation_elapsed_time == self.end_time:
        #     assert self.extancy == "current" or self.extancy == "post", message
        # else:
        #     assert self.extancy == "current", message
        diff_start_time = simulation_elapsed_time - self.start_time
        diff_end_time = simulation_elapsed_time - self.end_time
        epsilon = 1e-6
        # print(diff_start_time)
        # print(diff_end_time)
        if diff_start_time < -epsilon:
            assert self.extancy == "pre", message
        elif diff_end_time > epsilon:
            assert self.extancy == "post", message
        elif abs(diff_start_time) < epsilon:
            assert self.extancy == "current" or self.extancy == "pre", message
        elif abs(diff_end_time) < epsilon:
            assert self.extancy == "current" or self.extancy == "post", message
        else:
            assert self.extancy == "current", message

    def debug_check_distribution(self, simulation_elapsed_time):
        if self.extancy == "current":
            assert utility.is_in_range(simulation_elapsed_time, self.start_time, self.end_time,), "{}: {} <= {} <= {}: False".format(lineage_id, self.start_time, simulation_elapsed_time, self.end_time)
            if self.debug_mode:
                for area_idx, presence in enumerate(self._current_distribution_check_bitlist):
                    area = self.host_system.areas[area_idx]
                    if presence == "1":
                        assert self.host_system.areas[area_idx] in self._current_areas, "{}".format(self.lineage_id)
                        assert self in area.host_lineages
                    elif presence == "0":
                        assert self.host_system.areas[area_idx] not in self._current_areas
                        assert self not in area.host_lineages
                    else:
                        raise ValueError
            else:
                for area in self.host_system.areas:
                    if area in self._current_areas:
                        assert self in area.host_lineages
                    else:
                        assert self not in area.host_lineages
        else:
            if simulation_elapsed_time is None:
                pass
            else:
                if self.extancy == "pre":
                    assert simulation_elapsed_time <= self.start_time
                    assert simulation_elapsed_time <= self.end_time
                elif self.extancy == "post":
                    assert simulation_elapsed_time >= self.start_time
                    assert abs(simulation_elapsed_time - self.end_time) < 1e-6 or simulation_elapsed_time >= self.end_time, "False: {} >= {}".format(simulation_elapsed_time, self.end_time)
                else:
                    raise ValueError(self.extancy)
            for area in self.host_system.areas:
                assert self not in area.host_lineages

    # def area_iter(self):
    #     """
    #     Iterate over areas in which this host occurs.
    #     """
    #     for area_idx in self.current_distribution_bitvector:
    #         if self.current_distribution_bitvector[area_idx] == 1:
    #             yield self.host_system.areas[area_idx]

    # def add_area(self, area_idx):
    #     self.current_distribution_bitvector[area_idx] = 1

    # def remove_area(self, area_idx):
    #     self.current_distribution_bitvector[area_idx] = 0

    # def has_area(self, area_idx):
    #     return self.current_distribution_bitvector[area_idx] == 1

class HostSystem(object):
    """
    Models the the collection of hosts for a particular simulation replicate,
    based on a HostHistory. The HostHistory provides the basic invariant
    definitions/rules that apply across all replicates, while the HostSystem
    tracks state etc. for a single replicate.
    """

    def __init__(self,
            host_history,
            host_to_symbiont_time_scale_factor=1.0,
            debug_mode=False,
            run_logger=None):
        self.compile(
                host_history=host_history,
                host_to_symbiont_time_scale_factor=host_to_symbiont_time_scale_factor,
                debug_mode=debug_mode,
                )
        self._next_host_event = None

    def compile(self, host_history, host_to_symbiont_time_scale_factor, debug_mode=False):
        self.host_history = host_history
        self.host_tree = self.host_history.tree
        self.host_lineage_distance_matrix = self.host_history.lineage_distance_matrix
        self.host_to_symbiont_time_scale_factor = host_to_symbiont_time_scale_factor
        self.start_time = self.host_history.start_time * self.host_to_symbiont_time_scale_factor
        self.end_time = self.host_history.end_time * self.host_to_symbiont_time_scale_factor

        # build areas
        self.areas = self.host_history.generate_areas()
        self.num_areas = len(self.areas)

        # self.area_host_symbiont_host_area_distribution = {}
        # for area in self.areas:
        #     self.area_host_symbiont_host_area_distribution[area] = {}
        #     for host_lineage in self.host_lineages_by_id.values():
        #         self.area_host_symbiont_host_area_distribution[area][host_lineage] = {}

        # compile lineages
        self.host_lineages = set()
        self.host_lineages_by_id = {}
        self.leaf_host_lineages = set()
        self.extant_leaf_host_lineages = set()
        self.seed_host_lineage = None
        for host_history_lineage_id_definition in self.host_history.lineages.values():
            host = HostLineage(
                    host_history_lineage_definition=host_history_lineage_id_definition,
                    host_system=self,
                    debug_mode=debug_mode,
                    )
            self.host_lineages.add(host)
            self.host_lineages_by_id[host.lineage_id] = host
            if host.is_seed_node:
                assert self.seed_host_lineage is None
                self.seed_host_lineage = host
            if host.is_leaf:
                self.leaf_host_lineages.add(host)
            if host.is_extant_leaf:
                self.extant_leaf_host_lineages.add(host)

        # local copy of host events
        # self.host_events = list(self.host_history.events)
        self.host_events = []
        for event in self.host_history.events:
            event_copy = HostHistory.HostEvent(
                event_time=event.event_time * self.host_to_symbiont_time_scale_factor,
                weight=event.weight,
                lineage_id=event.lineage_id,
                event_type=event.event_type,
                event_subtype=event.event_subtype,
                area_idx=event.area_idx,
                child0_lineage_id=event.child0_lineage_id,
                child1_lineage_id=event.child1_lineage_id,
                )
            self.host_events.append(event_copy)

    def extant_host_lineages_at_current_time(self, current_time):
        ## TODO: if we hit this often, we need to construct a look-up table
        lineages = []
        for host_lineage in self.host_lineages_by_id.values():
            if host_lineage.start_time <= current_time and host_lineage.end_time >= current_time:
                lineages.append(host_lineage)
        return lineages

    def debug_check(self, simulation_elapsed_time=None):
        for host_lineage in self.host_lineages:
            host_lineage.debug_check(simulation_elapsed_time=simulation_elapsed_time)
            for area in host_lineage._current_areas:
                assert area in self.areas
        for event in self.host_events:
            area_idx = event.area_idx
            if area_idx is None:
                continue
            assert self.areas[area_idx].area_idx == area_idx

class SymbiontLineage(dendropy.Node):
    """
    A symbiont lineage.
    """

    class NullDistributionException(Exception):
        def __init__(self, lineage):
            self.lineage = lineage

    def __init__(self, index, host_system):

        dendropy.Node.__init__(self)
        self.index = index
        self.host_system = host_system
        self.is_extant = True
        self.edge.length = 0

        # distribution tracking/management
        self._host_area_distribution = {}
        for host_lineage in self.host_system.host_lineages:
            self._host_area_distribution[host_lineage] = {}
            for area in self.host_system.areas:
                self._host_area_distribution[host_lineage][area] = 0

        ## For quick look-up if host/area is infected
        self._infected_hosts = set()
        self._infected_areas = set()

    def host_occurrences_bitstring(self):
        s = []
        for host in self.host_system.host_lineages:
            if host in self._infected_hosts:
                s.append("1")
            else:
                s.append("0")
        return "".join(s)

    def add_host_in_area(self, host_lineage, area=None):
        """
        Adds a host to the distribution.
        If ``area`` is specified, then only the host in a specific area is infected.
        Otherwise, all hosts (of the given lineage) in all areas are infected.
        """
        if area is None:
            for area in host_lineage.current_area_iter():
                self._host_area_distribution[host_lineage][area] = 1
                area.symbiont_lineages.add(self)
                self._infected_areas.add(area)
        else:
            assert host_lineage.has_area(area)
            self._host_area_distribution[host_lineage][area] = 1
            area.symbiont_lineages.add(self)
            self._infected_areas.add(area)
        self._infected_hosts.add(host_lineage)

    def remove_host_in_area(self, host_lineage, area=None):
        """
        Removes a host from the distribution.
        If ``area_idx`` is specified, then only the host in that specific area is removed. Otherwise,
        Otherwise, all hosts (of the given lineage) of all areas are removed from the range.
        """
        if area is None:
            self.remove_host(host_lineage)
        else:
            assert host_lineage.has_area(area), "{} not in host area: {}".format(area, host_lineage._current_areas)
            self._host_area_distribution[host_lineage][area] = 0
            self.sync_area_cache(area)
            self.sync_host_cache(host_lineage)
            self.check_for_null_distribution()

    def remove_host(self, host_lineage):
        """
        Removes association with host from all areas.
        """
        for area in host_lineage.current_area_iter():
            if self._host_area_distribution[host_lineage][area] > 0:
                self._host_area_distribution[host_lineage][area] = 0
                self.sync_area_cache(area)
        self._infected_hosts.remove(host_lineage)
        self.check_for_null_distribution()

    def sync_area_cache(self, area, search_all_hosts=False):
        """
        Check if symbiont occurs in any host in the given area.
        If not, remove association with area.
        Otherwise, add it.
        If ``search_all_hosts`` is True, then all hosts in the system will be searched.
        Otherwise, only hosts known to be associated with this area will be searched.
        """
        if search_all_hosts:
            host_lineage_iter = iter(self.host_system.host_lineages)
        else:
            host_lineage_iter = iter(area.host_lineages)
        for host_lineage in area.host_lineages:
            if self._host_area_distribution[host_lineage][area] == 1:
                self._infected_areas.add(area)
                area.symbiont_lineages.add(self)
                break
        else:
            self._infected_areas.remove(area)
            area.symbiont_lineages.remove(self)

    def sync_host_cache(self, host_lineage, search_all_areas=False):
        """
        Check if symbiont occurs in the given host in any of its areas.
        If not, remove association with host.
        Otherwise, add it.
        If ``search_all_areas`` is True, then all areas in the system will be searched.
        Otherwise, only areas known to be associated with this host will be searched.
        """
        if search_all_areas:
            area_iter = iter(self.host_system.areas)
        else:
            area_iter = host_lineage.current_area_iter()
        for area in area_iter:
            if self._host_area_distribution[host_lineage][area] == 1:
                self._infected_hosts.add(host_lineage)
                break
        else:
            self._infected_hosts.remove(host_lineage)

    def check_for_null_distribution(self):
        """
        Ensures that lineage occurs at least in one host in one area.
        """
        if not self._infected_hosts or not self._infected_areas:
            raise SymbiontLineage.NullDistributionException(lineage=self)

    def has_host(self, host_lineage):
        """
        Returns True if host is infected in any of its areas.
        """
        return host_lineage in self._infected_hosts

    def has_host_in_area(self, host_lineage, area):
        """
        Returns True if host is infected in a particular area.
        """
        return self._host_area_distribution[host_lineage][area] == 1

    def host_iter(self):
        """
        Iterates over hosts in which lineage occurs.
        """
        for host in self._infected_hosts:
            yield host

    def area_iter(self):
        """
        Iterates over areas in which lineage occurs.
        """
        for area in self._infected_areas:
            yield area

    def areas_in_host_iter(self, host_lineage):
        """
        Iterates over areas in which lineage is associated with a particular host.
        """
        for area in self._host_area_distribution[host_lineage]:
            if self._host_area_distribution[host_lineage][area] == 1:
                yield area
            else:
                assert self._host_area_distribution[host_lineage][area] == 0

    def has_area(self, area):
        """
        Returns True if area is infected.
        """
        return area in self._infected_areas

    def clear_distribution(self):
        """
        Clears out all host/area associations.
        """
        for host_lineage in self._host_area_distribution:
            for area in self._host_area_distribution[host_lineage]:
                self._host_area_distribution[host_lineage][area] = 0
        for area in self._infected_areas:
            area.symbiont_lineages.remove(self)
        self._infected_hosts = set()
        self._infected_areas = set()

    def update_distribution(self, other):
        """
        Adds all host/area associations in ``other`` to self.
        """
        for host_lineage in other._host_area_distribution:
            for area in other._host_area_distribution[host_lineage]:
                if other._host_area_distribution[host_lineage][area] == 1 and self._host_area_distribution[host_lineage][area] == 0:
                    self.add_host_in_area(host_lineage=host_lineage, area=area)
        self._infected_hosts.update(other._infected_hosts)
        self._infected_areas.update(other._infected_areas)

    def debug_check(self, simulation_elapsed_time=None, ignore_nonextant_host_check_fail=False):
        # check that, as an extant lineage, it occupies at least
        # one host/area
        infected_hosts = set()
        infected_areas = set()
        noninfected_hosts = set()
        noninfected_areas = set([area for area in self.host_system.areas])
        occurrences = 0
        for host_lineage in self.host_system.host_lineages:
            for area in self.host_system.areas:
                if self._host_area_distribution[host_lineage][area] == 1:
                    infected_hosts.add(host_lineage)
                    infected_areas.add(area)
                    assert host_lineage in area.host_lineages
                    assert self in area.symbiont_lineages
                    noninfected_areas.discard(area)
                    noninfected_hosts.discard(host_lineage)
                    occurrences += 1
                elif self._host_area_distribution[host_lineage][area] != 0:
                    raise ValueError(self._host_area_distribution[host_lineage][area])
        assert occurrences > 0
        # check that the caches are in sync
        assert infected_hosts == self._infected_hosts
        assert infected_areas == self._infected_areas, "{} != {}".format([a.area_idx for a in infected_areas], [a.area_idx for a in self._infected_areas])
        for area in noninfected_areas:
            assert self not in area.symbiont_lineages
        # check that the infected hosts are supposed to exist at the current time
        if simulation_elapsed_time is not None:
            for host_lineage in self._infected_hosts:
                host_lineage.assert_correctly_extant(
                        simulation_elapsed_time,
                        ignore_fail=ignore_nonextant_host_check_fail)

class SymbiontPhylogeny(dendropy.Tree):

    def node_factory(cls, **kwargs):
        return SymbiontLineage(**kwargs)
    node_factory = classmethod(node_factory)

    def __init__(self, *args, **kwargs):
        self.model = kwargs.pop("model")
        self.model_id = self.model.model_id
        self.host_system = kwargs.pop("host_system")
        self.annotations.add_bound_attribute("model_id")
        self.rng = kwargs.pop("rng")
        self.debug_mode = kwargs.pop("debug_mode")
        self.run_logger = kwargs.pop("run_logger")
        self.lineage_indexer = utility.IndexGenerator(0)
        if "seed_node" not in kwargs:
            seed_node = self.node_factory(
                    index=next(self.lineage_indexer),
                    host_system=self.host_system,
                    )
            kwargs["seed_node"] = seed_node
        dendropy.Tree.__init__(self, *args, **kwargs)
        self.current_lineages = set([self.seed_node])

    def __deepcopy__(self, memo=None):
        raise NotImplementedError
        # if memo is None:
        #     memo = {}
        # memo[id(self.model)] = self.model
        # memo[id(self.rng)] = None #self.rng
        # memo[id(self.run_logger)] = self.run_logger
        # memo[id(self.taxon_namespace)] = self.taxon_namespace
        # return dendropy.Tree.__deepcopy__(self, memo)

    def current_lineage_iter(self):
        for lineage in self.current_lineages:
            yield lineage

    def split_lineage(self, symbiont_lineage):
        c1 = self.node_factory(
                index=next(self.lineage_indexer),
                host_system=self.host_system,
                )
        c2 = self.node_factory(
                index=next(self.lineage_indexer),
                host_system=self.host_system,
                )
        ### TODO: implement actual cladogenetic host/area inheritence logic
        ### Current: both daughters inherit parent host/area distribution
        for ch in (c1, c2):
            ch.update_distribution(symbiont_lineage)

        # if self.debug_mode:
        #     self.run_logger.debug("Splitting {} with distribution {} under speciation mode {} to: {} (distribution: {}) and {} (distribution: {})".format(
        #         symbiont_lineage,
        #         symbiont_lineage.distribution_vector.presences(),
        #         speciation_mode,
        #         c1,
        #         dist1.presences(),
        #         c2,
        #         dist2.presences(),
        #         ))
        #     assert len(dist1.presences()) > 0
        #     assert len(dist2.presences()) > 0

        symbiont_lineage.is_extant = False
        self.current_lineages.remove(symbiont_lineage)
        symbiont_lineage.clear_distribution()
        symbiont_lineage.add_child(c1)
        symbiont_lineage.add_child(c2)
        self.current_lineages.add(c1)
        self.current_lineages.add(c2)

    def extinguish_lineage(self, symbiont_lineage):
        self._make_lineage_extinct_on_phylogeny(symbiont_lineage)

    # def contract_lineage_host_set(self, symbiont_lineage, host_lineage, area):
    #     pass

    # def expand_lineage_area_set(self, symbiont_lineage, host_lineage, area):
    #     pass

    # def contract_lineage_area_set(self, symbiont_lineage, host_lineage, area):
    #     pass

    def _make_lineage_extinct_on_phylogeny(self, symbiont_lineage):
        if len(self.current_lineages) == 1:
            self.total_extinction_exception("no extant lineages remaining")
        symbiont_lineage.is_extant = False
        self.current_lineages.remove(symbiont_lineage)
        self.prune_subtree(symbiont_lineage)

    def total_extinction_exception(self, msg):
        # self.run_logger.info("Total extinction: {}".format(msg))
        raise error.TotalExtinctionException(msg)

    # def evolve_trait(self, lineage, trait_idx, state_idx):
    #     lineage.traits_vector[trait_idx] = state_idx

    def disperse_lineage(self, lineage, dest_area_idx):
        lineage.distribution_vector[dest_area_idx] = 1

    # def focal_area_lineages(self):
    #     focal_area_lineages = set()
    #     for lineage in self.current_lineage_iter():
    #         for area_idx in self.model.geography.focal_area_indexes:
    #             if lineage.distribution_vector[area_idx] == 1:
    #                 focal_area_lineages.add(lineage)
    #                 break
    #     return focal_area_lineages

    # def num_focal_area_lineages(self):
    #     count = 0
    #     for lineage in self.current_lineage_iter():
    #         for area_idx in self.model.geography.focal_area_indexes:
    #             if lineage.distribution_vector[area_idx] == 1:
    #                 count += 1
    #                 break
    #     return count

    # def extract_focal_areas_tree(self):
    #     # tcopy = SymbiontPhylogeny(self)
    #     tcopy = copy.deepcopy(self)
    #     focal_area_lineages = tcopy.focal_area_lineages()
    #     if len(focal_area_lineages) < 2:
    #         raise error.InsufficientFocalAreaLineagesSimulationException("insufficient lineages in focal area at termination".format(len(focal_area_lineages)))
    #     try:
    #         tcopy.filter_leaf_nodes(filter_fn=lambda x: x in focal_area_lineages)
    #     except dendropy.SeedNodeDeletionException:
    #         raise error.InsufficientFocalAreaLineagesSimulationException("no extant lineages in focal area at termination".format(len(focal_area_lineages)))
    #     return tcopy

class InphestModel(object):

    _TRAITS_SEPARATOR = "."
    _LABEL_COMPONENTS_SEPARATOR = "^"
    _NULL_TRAITS = "NA"

    @classmethod
    def create(
            cls,
            model_definition_source,
            model_definition_type,
            interpolate_missing_model_values=False,
            run_logger=None,
            ):
        """
        Create and return a model under which to run a simulation.

        Parameters
        ----------
        model_definition_source : object
            See 'model_definition_type' argument for values this can take.
        model_definition_type : str
            Whether 'model_definition_source' is:

                - 'python-dict' : a Python dictionary defining the model.
                - 'python-dict-str' : a string providing a Python dictionary
                  defining the model.
                - 'python-dict-filepath' : a path to a Python file to be evaluated;
                  the file should be a valid Python script containing nothing but a
                  dictionary defining the model.
                - 'json-filepath': a path to a JSON file containing a dictionary
                  defining the model.

        Returns
        -------
        m : ArchipelagoModel
            A fully-specified Archipelago model.

        """
        if model_definition_type == "python-dict-filepath":
            src = open(model_definition_source, "r")
            model_definition = eval(src.read())
        elif model_definition_type == "python-dict-str":
            model_definition = eval(model_definition_source)
        elif model_definition_type == "python-dict":
            model_definition = model_definition_source
        elif model_definition_type == "json-filepath":
            src = open(model_definition_source, "r")
            model_definition = json.load(src)
        else:
            raise ValueError("Unrecognized model definition type: '{}'".format(model_definition_type))
        return cls.from_definition_dict(
                model_definition=model_definition,
                run_logger=run_logger,
                interpolate_missing_model_values=interpolate_missing_model_values)

    @classmethod
    def from_definition_dict(cls,
            model_definition,
            interpolate_missing_model_values=False,
            run_logger=None):
        archipelago_model = cls()
        archipelago_model.parse_definition(
                model_definition=model_definition,
                interpolate_missing_model_values=interpolate_missing_model_values,
                run_logger=run_logger,
        )
        return archipelago_model

    @staticmethod
    def compose_encoded_label(symbiont_lineage):
        return "s{lineage_index}{sep}{host_occurrences}".format(
                lineage_index=symbiont_lineage.index,
                sep=InphestModel._LABEL_COMPONENTS_SEPARATOR,
                host_occurrences=symbiont_lineage.host_occurrences_bitstring(),
                )
        return encoding

    @staticmethod
    def decode_label(label):
        raise NotImplementedError
        # parts = label.split(ArchipelagoModel._LABEL_COMPONENTS_SEPARATOR)
        # traits_string = parts[1]
        # if not traits_string or traits_string == ArchipelagoModel._NULL_TRAITS:
        #     traits_vector = StatesVector(nchar=0)
        # else:
        #     traits_string_parts = traits_string.split(ArchipelagoModel._TRAITS_SEPARATOR)
        #     traits_vector = StatesVector(
        #             nchar=len(traits_string_parts),
        #             # The trait states need to be an integer if
        #             # archipelago-summarize.py coerces the user input to
        #             # integers
        #             # values=[int(i) for i in traits_string_parts],
        #             # The reason we do NOT want it parsed to an integer value
        #             # is to allow null traits 'NA', 'null', etc.
        #             values=[i for i in traits_string_parts],
        #             )
        # distribution_string = parts[2]
        # distribution_vector = DistributionVector(
        #         num_areas=len(distribution_string),
        #         values=[int(i) for i in distribution_string],)
        # return traits_vector, distribution_vector

    @staticmethod
    def set_lineage_data(
            tree,
            leaf_nodes_only=False,
            lineage_data_source="node",
            traits_filepath=None,
            areas_filepath=None,
            ):
        raise NotImplementedError
        # if lineage_data_source == "node":
        #     _decode = lambda x: ArchipelagoModel.decode_label(x.label)
        # elif lineage_data_source == "taxon":
        #     _decode = lambda x: ArchipelagoModel.decode_label(x.taxon.label)
        # else:
        #     raise ValueError("'lineage_data_source' must be 'node' or 'taxon'")
        # for nd in tree:
        #     if (not leaf_nodes_only or not nd._child_nodes) and (lineage_data_source == "node" or nd.taxon is not None):
        #         traits_vector, distribution_vector = _decode(nd)
        #         nd.traits_vector = traits_vector
        #         nd.distribution_vector = distribution_vector
        #     else:
        #         nd.traits_vector = None
        #         nd.distribution_vector = None

    def __init__(self):
        pass

    def parse_definition(self,
            model_definition,
            run_logger=None,
            interpolate_missing_model_values=True):

        # initialize
        if model_definition is None:
            model_definition = {}
        else:
            model_definition = dict(model_definition)

        # model identification
        if "model_id" not in model_definition:
            model_definition["model_id"] = "Model1"
            if run_logger is not None:
                run_logger.warning("Model identifier not specified: defaulting to '{}'".format(model_definition["model_id"]))
        self.model_id = model_definition.pop("model_id", "Model1")
        if run_logger is not None:
            run_logger.info("Setting up model with identifier: '{}'".format(self.model_id))

        # timing
        self.host_to_symbiont_time_scale_factor = float(model_definition.pop("host_to_symbiont_time_scale_factor", 1.00))
        if run_logger is not None:
            run_logger.info("(TIME SCALE) Setting time scale: 1 unit of host time is equal to {} unit(s) of symbiont time".format(self.host_to_symbiont_time_scale_factor))

        # Diversification
        diversification_d = dict(model_definition.pop("diversification", {}))

        ## speciation
        self.mean_symbiont_lineage_birth_rate = diversification_d.pop("mean_symbiont_lineage_birth_rate", 0.03)
        if run_logger is not None:
            run_logger.info("(DIVERSIFICATION) Mean symbiont lineage diversification birth rate: {}".format(self.mean_symbiont_lineage_birth_rate))
        if "symbiont_lineage_birth_weight" in diversification_d:
            self.symbiont_lineage_birth_weight_function = RateFunction.from_definition_dict(diversification_d.pop("symbiont_lineage_birth_weight"))
        else:
            self.symbiont_lineage_birth_weight_function = RateFunction(
                    definition_type="lambda_definition",
                    definition_content="lambda **kwargs: 1.00",
                    description="fixed: 1.00",
                    )
        if run_logger is not None:
            run_logger.info("(DIVERSIFICATION) Setting symbiont lineage-specific birth weight function: {desc}".format(
                desc=self.symbiont_lineage_birth_weight_function.description,))

        ## extinction
        self.mean_symbiont_lineage_death_rate = diversification_d.pop("mean_symbiont_lineage_death_rate", 0.00)
        if run_logger is not None:
            run_logger.info("(DIVERSIFICATION) Mean symbiont lineage diversification death rate: {}".format(self.mean_symbiont_lineage_death_rate))
        if "symbiont_lineage_death_weight" in diversification_d:
            self.symbiont_lineage_death_weight_function = RateFunction.from_definition_dict(diversification_d.pop("symbiont_lineage_death_weight"))
        else:
            self.symbiont_lineage_death_weight_function = RateFunction(
                    definition_type="lambda_definition",
                    definition_content="lambda **kwargs: 1.00",
                    description="fixed: 1.00",
                    )
        if run_logger is not None:
            run_logger.info("(DIVERSIFICATION) Setting symbiont lineage-specific death weight function: {desc}".format(
                desc=self.symbiont_lineage_death_weight_function.description,))

        # Host Submodel

        ## Anagenetic Host Evolution Submodel
        anagenetic_host_assemblage_evolution_d = dict(model_definition.pop("anagenetic_host_assemblage_evolution", {}))

        ### Anagenetic Host Gain
        self.mean_symbiont_lineage_host_gain_rate = anagenetic_host_assemblage_evolution_d.pop("mean_symbiont_lineage_host_gain_rate", 0.03)
        if run_logger is not None:
            run_logger.info("(ANAGENETIC HOST ASSEMBLAGE EVOLUTION) Setting mean host gain rate: {desc}".format(
                desc=self.mean_symbiont_lineage_host_gain_rate,))
        if "symbiont_lineage_host_gain_weight" in anagenetic_host_assemblage_evolution_d:
            self.symbiont_lineage_host_gain_weight_function = RateFunction.from_definition_dict(anagenetic_host_assemblage_evolution_d.pop("symbiont_lineage_host_gain_weight"))
        else:
            self.symbiont_lineage_host_gain_weight_function = RateFunction(
                    definition_type="lambda_definition",
                    definition_content="lambda **kwargs: 1.00",
                    description="fixed: 1.00",
                    )
        if run_logger is not None:
            run_logger.info("(ANAGENETIC HOST ASSEMBLAGE EVOLUTION) Setting symbiont lineage-specific host gain weight function: {desc}".format(
                desc=self.symbiont_lineage_host_gain_weight_function.description,))

        ### Anagenetic Host Loss
        self.mean_symbiont_lineage_host_loss_rate = anagenetic_host_assemblage_evolution_d.pop("mean_symbiont_lineage_host_loss_rate", 0.00)
        if run_logger is not None:
            run_logger.info("(ANAGENETIC HOST ASSEMBLAGE EVOLUTION) Setting mean host loss rate: {desc}".format(
                desc=self.mean_symbiont_lineage_host_loss_rate,))
        if "symbiont_lineage_host_loss_weight" in anagenetic_host_assemblage_evolution_d:
            self.symbiont_lineage_host_loss_weight_function = RateFunction.from_definition_dict(anagenetic_host_assemblage_evolution_d.pop("symbiont_lineage_host_loss_weight"))
        else:
            self.symbiont_lineage_host_loss_weight_function = RateFunction(
                    definition_type="lambda_definition",
                    definition_content="lambda **kwargs: 1.0",
                    description="fixed: 1.0",
                    )
        if run_logger is not None:
            run_logger.info("(ANAGENETIC HOST ASSEMBLAGE EVOLUTION) Setting symbiont lineage-specific host loss weight function: {desc}".format(
                desc=self.symbiont_lineage_host_loss_weight_function.description,
                ))

        if anagenetic_host_assemblage_evolution_d:
            raise TypeError("Unsupported keywords in anagenetic host range evolution submodel: {}".format(anagenetic_host_assemblage_evolution_d))

        ## Cladogenetic Host Evolution Submodel
        cladogenetic_host_assemblage_evolution = dict(model_definition.pop("cladogenetic_host_assemblage_evolution", {}))
        self.host_cladogenesis_sympatric_subset_speciation_weight = float(cladogenetic_host_assemblage_evolution.pop("sympatric_subset_speciation_weight", 1.0))
        self.host_cladogenesis_single_host_vicariance_speciation_weight = float(cladogenetic_host_assemblage_evolution.pop("single_host_vicariance_speciation_weight", 1.0))
        self.host_cladogenesis_widespread_vicariance_speciation_weight = float(cladogenetic_host_assemblage_evolution.pop("widespread_vicariance_speciation_weight", 1.0))
        self.host_cladogenesis_founder_event_speciation_weight = float(cladogenetic_host_assemblage_evolution.pop("founder_event_speciation_weight", 0.0))
        if cladogenetic_host_assemblage_evolution:
            raise TypeError("Unsupported keywords in cladogenetic range evolution submodel: {}".format(cladogenetic_host_assemblage_evolution))
        if run_logger is not None:
            run_logger.info("(CLADOGENETIC HOST ASSEMBLAGE EVOLUTION) Base weight of sympatric subset speciation mode: {}".format(self.host_cladogenesis_sympatric_subset_speciation_weight))
            run_logger.info("(CLADOGENETIC HOST ASSEMBLAGE EVOLUTION) Base weight of single host vicariance speciation mode: {}".format(self.host_cladogenesis_single_host_vicariance_speciation_weight))
            run_logger.info("(CLADOGENETIC HOST ASSEMBLAGE EVOLUTION) Base weight of widespread vicariance speciation mode: {}".format(self.host_cladogenesis_widespread_vicariance_speciation_weight))
            run_logger.info("(CLADOGENETIC HOST ASSEMBLAGE EVOLUTION) Base weight of founder event speciation ('jump dispersal') mode: {} (note that the effective weight of this event for each lineage is actually the product of this and the lineage-specific host gain weight)".format(self.host_cladogenesis_founder_event_speciation_weight))

        # Geographical Range Evolution Submodel

        ## Anagenetic Geographical Range Evolution Submodel

        ### Anagenetic Geographical Area Gain

        anagenetic_geographical_range_evolution_d = dict(model_definition.pop("anagenetic_geographical_range_evolution", {}))
        self.mean_symbiont_lineage_area_gain_rate = anagenetic_geographical_range_evolution_d.pop("mean_symbiont_lineage_area_gain_rate", 0.03)
        if run_logger is not None:
            run_logger.info("(ANAGENETIC HOST ASSEMBLAGE EVOLUTION) Setting mean area gain rate: {desc}".format(
                desc=self.mean_symbiont_lineage_host_gain_rate,))
        if "symbiont_lineage_area_gain_weight" in anagenetic_geographical_range_evolution_d:
            self.symbiont_lineage_area_gain_weight_function = RateFunction.from_definition_dict(anagenetic_geographical_range_evolution_d.pop("symbiont_lineage_area_gain_weight"))
        else:
            self.symbiont_lineage_area_gain_weight_function = RateFunction(
                    definition_type="lambda_definition",
                    definition_content="lambda **kwargs: 1.00",
                    description="fixed: 1.00",
                    )
        if run_logger is not None:
            run_logger.info("(ANAGENETIC GEOGRAPHICAL RANGE EVOLUTION) Setting symbiont lineage-specific area gain weight function: {desc}".format(
                desc=self.symbiont_lineage_area_gain_weight_function.description,))

        ### Anagenetic Geographical Area Loss
        anagenetic_geographical_range_evolution_d = dict(model_definition.pop("anagenetic_geographical_range_evolution", {}))
        self.mean_symbiont_lineage_area_loss_rate = anagenetic_geographical_range_evolution_d.pop("mean_symbiont_lineage_area_loss_rate", 0.0)
        if run_logger is not None:
            run_logger.info("(ANAGENETIC HOST ASSEMBLAGE EVOLUTION) Setting mean area loss rate: {desc}".format(
                desc=self.mean_symbiont_lineage_host_loss_rate,))
        if "symbiont_lineage_area_loss_weight" in anagenetic_geographical_range_evolution_d:
            self.symbiont_lineage_area_loss_weight_function = RateFunction.from_definition_dict(anagenetic_geographical_range_evolution_d.pop("symbiont_lineage_area_loss_weight"))
        else:
            self.symbiont_lineage_area_loss_weight_function = RateFunction(
                    definition_type="lambda_definition",
                    definition_content="lambda **kwargs: 1.0",
                    description="fixed: 1.0",
                    )
        if run_logger is not None:
            run_logger.info("(ANAGENETIC GEOGRAPHICAL RANGE EVOLUTION) Setting symbiont lineage-specific area loss weight function: {desc}".format(
                desc=self.symbiont_lineage_area_loss_weight_function.description,
                ))

        if anagenetic_geographical_range_evolution_d:
            raise TypeError("Unsupported keywords in anagenetic geographical range evolution submodel: {}".format(anagenetic_geographical_range_evolution_d))

        ## Cladogenetic Geographical Evolution Submodel

        symbiont_cladogenetic_geographical_range_evolution_d = dict(model_definition.pop("cladogenetic_geographical_range_evolution", {}))
        self.symbiont_cladogenesis_sympatric_subset_speciation_weight = float(symbiont_cladogenetic_geographical_range_evolution_d.pop("sympatric_subset_speciation_weight", 1.0))
        self.symbiont_cladogenesis_single_area_vicariance_speciation_weight = float(symbiont_cladogenetic_geographical_range_evolution_d.pop("single_area_vicariance_speciation_weight", 1.0))
        self.symbiont_cladogenesis_widespread_vicariance_speciation_weight = float(symbiont_cladogenetic_geographical_range_evolution_d.pop("widespread_vicariance_speciation_weight", 1.0))
        self.symbiont_cladogenesis_founder_event_speciation_weight = float(symbiont_cladogenetic_geographical_range_evolution_d.pop("founder_event_speciation_weight", 0.0))
        if symbiont_cladogenetic_geographical_range_evolution_d:
            raise TypeError("Unsupported keywords in cladogenetic geographical range evolution submodel: {}".format(symbiont_cladogenetic_geographical_range_evolution_d))
        if run_logger is not None:
            run_logger.info("(CLADOGENETIC GEOGRAPHICAL RANGE EVOLUTION) Base weight of sympatric subset speciation mode: {}".format(self.symbiont_cladogenesis_sympatric_subset_speciation_weight))
            run_logger.info("(CLADOGENETIC GEOGRAPHICAL RANGE EVOLUTION) Base weight of single area vicariance speciation mode: {}".format(self.symbiont_cladogenesis_single_area_vicariance_speciation_weight))
            run_logger.info("(CLADOGENETIC GEOGRAPHICAL RANGE EVOLUTION) Base weight of widespread vicariance speciation mode: {}".format(self.symbiont_cladogenesis_widespread_vicariance_speciation_weight))
            run_logger.info("(CLADOGENETIC GEOGRAPHICAL RANGE EVOLUTION) Base weight of founder event speciation ('jump dispersal') mode: {} (note that the effective weight of this event for each lineage is actually the product of this and the lineage-specific area gain weight)".format(self.symbiont_cladogenesis_founder_event_speciation_weight))

        if model_definition:
            raise TypeError("Unsupported model keywords: {}".format(model_definition))

    # def encode_lineage(self,
    #         symbiont_lineage,
    #         set_label=False,
    #         add_annotation=False,
    #         ):
    #     encoded_label = InphestModel.compose_encoded_label(symbiont_lineage=symbiont_lineage)
    #     if set_label:
    #         lineage.label =encoded_label
    #     if add_annotation:
    #         lineage.annotations.drop()
    #         lineage.annotations.add_new("traits_v", traits_v)
    #         lineage.annotations.add_new("distribution", areas_v)
    #         for trait_idx, trait in enumerate(self.trait_types):
    #             lineage.annotations.add_new(trait.label, lineage.traits_vector[trait_idx])
    #         area_list = []
    #         for area_idx, area in enumerate(self.geography.areas):
    #             if exclude_supplemental_areas and area.is_supplemental:
    #                 continue
    #             if lineage.distribution_vector[area_idx] == 1:
    #                 area_list.append(area.label)
    #         lineage.annotations.add_new("areas", area_list)
    #     return encoded_label

    def write_model(self, out):
        model_definition = collections.OrderedDict()
        model_definition["model_id"] = self.model_id
        model_definition["host_to_symbiont_time_scale_factor"] = self.host_to_symbiont_time_scale_factor
        model_definition["diversification"] = self.diversification_as_definition()
        model_definition["anagenetic_host_assemblage_evolution"] = self.anagenetic_host_assemblage_evolution_as_definition()
        model_definition["cladogenetic_host_assemblage_evolution"] = self.cladogenetic_host_assemblage_evolution_as_definition()
        model_definition["anagenetic_geographical_range_evolution"] = self.anagenetic_geographical_range_evolution_as_definition()
        model_definition["cladogenetic_geographical_range_evolution"] = self.cladogenetic_geographical_range_evolution_as_definition()
        json.dump(model_definition, out, indent=4, separators=(',', ': '))
        out.flush()

    def diversification_as_definition(self):
        d = collections.OrderedDict()
        d["mean_symbiont_lineage_birth_rate"] = self.mean_symbiont_lineage_birth_rate
        d["lineage_birth_rate"] = self.symbiont_lineage_birth_weight_function.as_definition()
        d["mean_symbiont_lineage_death_rate"] = self.mean_symbiont_lineage_death_rate
        d["lineage_death_rate"] = self.symbiont_lineage_death_weight_function.as_definition()
        return d

    def anagenetic_host_assemblage_evolution_as_definition(self):
        d = collections.OrderedDict()
        d["mean_symbiont_lineage_host_gain_rate"] = self.mean_symbiont_lineage_host_gain_rate
        d["symbiont_lineage_host_gain_weight"] = self.symbiont_lineage_host_gain_weight_function.as_definition()
        d["mean_symbiont_lineage_host_loss_rate"] = self.mean_symbiont_lineage_host_loss_rate
        d["symbiont_lineage_host_loss_weight"] = self.symbiont_lineage_host_loss_weight_function.as_definition()
        return d

    def cladogenetic_host_assemblage_evolution_as_definition(self):
        d = collections.OrderedDict()
        d["sympatric_subset_speciation_weight"] = self.host_cladogenesis_sympatric_subset_speciation_weight
        d["single_host_vicariance_speciation_weight"] = self.host_cladogenesis_single_host_vicariance_speciation_weight
        d["widespread_vicariance_speciation_weight"] = self.host_cladogenesis_widespread_vicariance_speciation_weight
        d["founder_event_speciation_weight"] = self.host_cladogenesis_founder_event_speciation_weight
        return d

    def anagenetic_geographical_range_evolution_as_definition(self):
        d = collections.OrderedDict()
        d["mean_symbiont_lineage_area_gain_rate"] = self.mean_symbiont_lineage_area_gain_rate
        d["symbiont_lineage_area_gain_weight"] = self.symbiont_lineage_area_gain_weight_function.as_definition()
        d["mean_symbiont_lineage_area_loss_rate"] = self.mean_symbiont_lineage_area_loss_rate
        d["symbiont_lineage_area_loss_weight"] = self.symbiont_lineage_area_loss_weight_function.as_definition()
        return d

    def cladogenetic_geographical_range_evolution_as_definition(self):
        d = collections.OrderedDict()
        d["sympatric_subset_speciation_weight"] = self.symbiont_cladogenesis_sympatric_subset_speciation_weight
        d["single_area_vicariance_speciation_weight"] = self.symbiont_cladogenesis_single_area_vicariance_speciation_weight
        d["widespread_vicariance_speciation_weight"] = self.symbiont_cladogenesis_widespread_vicariance_speciation_weight
        d["founder_event_speciation_weight"] = self.symbiont_cladogenesis_founder_event_speciation_weight
        return d

