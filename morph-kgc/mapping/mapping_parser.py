__author__ = "Julián Arenas-Guerrero"
__credits__ = ["Julián Arenas-Guerrero"]

__license__ = "Apache-2.0"
__maintainer__ = "Julián Arenas-Guerrero"
__email__ = "arenas.guerrero.julian@outlook.com"


import rdflib
import logging
import sql_metadata
import pandas as pd
import numpy as np
import constants
import utils
import multiprocessing as mp

from mapping.mapping_constants import MAPPINGS_DATAFRAME_COLUMNS, MAPPING_PARSING_QUERY, JOIN_CONDITION_PARSING_QUERY
from mapping.mapping_partitioner import MappingPartitioner
from data_source import relational_source


def _mapping_to_rml(mapping_graph):
    """
    Replaces R2RML rules in in the graph with the corresponding RML rules.
    """

    # add RML and QL namespaces
    mapping_graph.bind('rml', rdflib.term.URIRef(constants.RML_NAMESPACE))
    mapping_graph.bind('ql', rdflib.term.URIRef(constants.QL_NAMESPACE))

    # add reference formulation and sql version for RDB sources
    query = 'SELECT ?logical_source ?x WHERE { ?logical_source <' + constants.R2RML_TABLE_NAME + '> ?x . } '
    for logical_source, _ in mapping_graph.query(query):
        mapping_graph.add((logical_source, rdflib.term.URIRef(constants.R2RML_SQL_VERSION),
                           rdflib.term.URIRef(constants.R2RML_SQL2008)))
    query = 'SELECT ?logical_source ?x WHERE { ?logical_source <' + constants.R2RML_SQL_QUERY + '> ?x . } '
    for logical_source, _ in mapping_graph.query(query):
        mapping_graph.add((logical_source, rdflib.term.URIRef(constants.R2RML_SQL_VERSION),
                           rdflib.term.URIRef(constants.R2RML_SQL2008)))
        mapping_graph.add((logical_source, rdflib.term.URIRef(constants.RML_REFERENCE_FORMULATION),
                           rdflib.term.URIRef(constants.QL_CSV)))

    # replace R2RML predicates with the equivalent RML predicates
    mapping_graph = utils.replace_predicates_in_graph(mapping_graph, constants.R2RML_LOGICAL_TABLE,
                                                      constants.RML_LOGICAL_SOURCE)
    mapping_graph = utils.replace_predicates_in_graph(mapping_graph, constants.R2RML_SQL_QUERY, constants.RML_QUERY)
    mapping_graph = utils.replace_predicates_in_graph(mapping_graph, constants.R2RML_COLUMN, constants.RML_REFERENCE)

    # remove R2RML classes
    mapping_graph.remove((None, rdflib.term.URIRef(constants.R2RML_R2RML_VIEW_CLASS), None))
    mapping_graph.remove((None, rdflib.term.URIRef(constants.R2RML_LOGICAL_TABLE_CLASS), None))


    return mapping_graph


def _expand_constant_shortcut_properties(mapping_graph):
    """
    Expand constant shortcut properties rr:subject, rr:predicate, rr:object and rr:graph.
    See R2RML specification (https://www.w3.org/2001/sw/rdb2rdf/r2rml/#constant).
    """

    constant_properties = [constants.R2RML_SUBJECT_MAP, constants.R2RML_PREDICATE_MAP,
                           constants.R2RML_OBJECT_MAP, constants.R2RML_GRAPH_MAP]
    constant_shortcuts = [constants.R2RML_SUBJECT_CONSTANT_SHORTCUT, constants.R2RML_PREDICATE_CONSTANT_SHORTCUT,
                          constants.R2RML_OBJECT_CONSTANT_SHORTCUT, constants.R2RML_GRAPH_CONSTANT_SHORTCUT]

    for constant_property, constant_shortcut in zip(constant_properties, constant_shortcuts):
        for s, o in mapping_graph.query('SELECT ?s ?o WHERE {?s <' + constant_shortcut + '> ?o .}'):
            blanknode = rdflib.BNode()
            mapping_graph.add((s, rdflib.term.URIRef(constant_property), blanknode))
            mapping_graph.add((blanknode, rdflib.term.URIRef(constants.R2RML_CONSTANT), o))

        mapping_graph.remove((None, rdflib.term.URIRef(constant_shortcut), None))

    return mapping_graph


def _rdf_class_to_pom(mapping_graph):
    """
    Replace rr:class definitions by predicate object maps.
    """

    query = 'SELECT ?tm ?c WHERE { ' \
            '?tm <' + constants.R2RML_SUBJECT_MAP + '> ?sm . ' \
            '?sm <' + constants.R2RML_CLASS + '> ?c . }'
    for tm, c in mapping_graph.query(query):
        blanknode = rdflib.BNode()
        mapping_graph.add((tm, rdflib.term.URIRef(constants.R2RML_PREDICATE_OBJECT_MAP), blanknode))
        mapping_graph.add((blanknode, rdflib.term.URIRef(constants.R2RML_OBJECT_CONSTANT_SHORTCUT), c))
        mapping_graph.add((blanknode, rdflib.term.URIRef(constants.R2RML_PREDICATE_CONSTANT_SHORTCUT), rdflib.RDF.type))

    mapping_graph.remove((None, rdflib.term.URIRef(constants.R2RML_CLASS), None))

    return mapping_graph


def _subject_graph_maps_to_pom(mapping_graph):
    """
    Move graph maps in subject maps to the predicate object maps of subject maps.
    """

    # add the graph maps in the subject maps to every predicate object map of the subject maps
    query = 'SELECT ?sm ?gm ?pom WHERE { ' \
            '?tm <' + constants.R2RML_SUBJECT_MAP + '> ?sm . ' \
            '?sm <' + constants.R2RML_GRAPH_MAP + '> ?gm . ' \
            '?tm <' + constants.R2RML_PREDICATE_OBJECT_MAP + '> ?pom . }'
    for sm, gm, pom in mapping_graph.query(query):
        mapping_graph.add((pom, rdflib.term.URIRef(constants.R2RML_GRAPH_MAP), gm))

    # remove the graph maps from the subject maps
    query = 'SELECT ?sm ?gm WHERE { ' \
            '?tm <' + constants.R2RML_SUBJECT_MAP + '> ?sm . ' \
            '?sm <' + constants.R2RML_GRAPH_MAP + '> ?gm . }'
    for sm, gm in mapping_graph.query(query):
        mapping_graph.remove((sm, rdflib.term.URIRef(constants.R2RML_GRAPH_MAP), gm))

    return mapping_graph


def _complete_pom_with_default_graph(mapping_graph):
    """
    Complete predicate object maps without graph maps with rr:defaultGraph.
    """

    query = 'SELECT DISTINCT ?tm ?pom WHERE { ' \
            '?tm <' + constants.R2RML_PREDICATE_OBJECT_MAP + '> ?pom . ' \
            'OPTIONAL { ?pom <' + constants.R2RML_GRAPH_MAP + '> ?gm . } . ' \
            'FILTER ( !bound(?gm) ) }'
    for tm, pom in mapping_graph.query(query):
        blanknode = rdflib.BNode()
        mapping_graph.add((pom, rdflib.term.URIRef(constants.R2RML_GRAPH_MAP), blanknode))
        mapping_graph.add((blanknode, rdflib.term.URIRef(constants.R2RML_CONSTANT),
                           rdflib.term.URIRef(constants.R2RML_DEFAULT_GRAPH)))

    return mapping_graph


def _complete_termtypes(mapping_graph):
    """
    Completes term types of mapping rules that do not have rr:termType property as indicated in R2RML specification
    (https://www.w3.org/2001/sw/rdb2rdf/r2rml/#termtype).
    """

    # add missing blanknode termtypes in the constant-valued object maps
    query = 'SELECT DISTINCT ?term_map ?constant WHERE { ' \
            '?term_map <' + constants.R2RML_CONSTANT + '> ?constant . ' \
            'OPTIONAL { ?term_map <' + constants.R2RML_TERM_TYPE + '> ?termtype . } . ' \
            'FILTER ( !bound(?termtype) && isBlank(?constant) ) }'
    for term_map, _ in mapping_graph.query(query):
        mapping_graph.add(
            (term_map, rdflib.term.URIRef(constants.R2RML_TERM_TYPE), rdflib.term.URIRef(constants.R2RML_BLANK_NODE)))

    # add missing literals termtypes in the constant-valued object maps
    query = 'SELECT DISTINCT ?term_map ?constant WHERE { ' \
            '?term_map <' + constants.R2RML_CONSTANT + '> ?constant . ' \
            'OPTIONAL { ?term_map <' + constants.R2RML_TERM_TYPE + '> ?termtype . } . ' \
            'FILTER ( !bound(?termtype) && isLiteral(?constant) ) }'
    for term_map, _ in mapping_graph.query(query):
        mapping_graph.add(
            (term_map, rdflib.term.URIRef(constants.R2RML_TERM_TYPE), rdflib.term.URIRef(constants.R2RML_LITERAL)))

    # add missing literal termtypes in the object maps
    query = 'SELECT DISTINCT ?om ?pom WHERE { ' \
            '?pom <' + constants.R2RML_OBJECT_MAP + '> ?om . ' \
            'OPTIONAL { ?om <' + constants.R2RML_TERM_TYPE + '> ?termtype . } . ' \
            'OPTIONAL { ?om <' + constants.RML_REFERENCE + '> ?column . } . ' \
            'OPTIONAL { ?om <' + constants.R2RML_LANGUAGE + '> ?language . } . ' \
            'OPTIONAL { ?om <' + constants.R2RML_DATATYPE + '> ?datatype . } . ' \
            'FILTER ( !bound(?termtype) && ( bound(?column) || bound(?language) || bound(?datatype) ) ) }'
    for om, _ in mapping_graph.query(query):
        mapping_graph.add(
            (om, rdflib.term.URIRef(constants.R2RML_TERM_TYPE), rdflib.term.URIRef(constants.R2RML_LITERAL)))

    # now all missing termtypes are IRIs
    for term_map_property in [constants.R2RML_SUBJECT_MAP, constants.R2RML_PREDICATE_MAP,
                              constants.R2RML_OBJECT_MAP, constants.R2RML_GRAPH_MAP]:
        query = 'SELECT DISTINCT ?term_map ?x WHERE { ' \
                '?x <' + term_map_property + '> ?term_map . ' \
                'OPTIONAL { ?term_map <' + constants.R2RML_TERM_TYPE + '> ?termtype . } . ' \
                'FILTER ( !bound(?termtype) ) }'
        for term_map, _ in mapping_graph.query(query):
            mapping_graph.add(
                (term_map, rdflib.term.URIRef(constants.R2RML_TERM_TYPE), rdflib.term.URIRef(constants.R2RML_IRI)))

    return mapping_graph


def _complete_rml_classes(mapping_graph):
    """
    TODO
    """

    return mapping_graph


def _remove_self_joins(mapping_graph):
    query = 'SELECT DISTINCT ?OM ?join_condition ?parentSM_template ?parentSM_constant ?parentSM_reference ' \
                           ' ?parentSM_termtype WHERE { ' \
            '?childTM <' + constants.R2RML_PREDICATE_OBJECT_MAP + '> ?POM . ' \
            '?POM <' + constants.R2RML_OBJECT_MAP + '> ?OM . ' \
            '?OM <' + constants.R2RML_PARENT_TRIPLES_MAP + '> ?parentTM . ' \
            '?parentTM <' + constants.R2RML_SUBJECT_MAP + '> ?parentSM . ' \
            'OPTIONAL { ?join_condition <' + constants.R2RML_JOIN_CONDITION + '> ?join_condition . } . ' \
            'OPTIONAL { ?parentSM <' + constants.R2RML_TEMPLATE + '> ?parentSM_template . } . ' \
            'OPTIONAL { ?parentSM <' + constants.R2RML_CONSTANT + '> ?parentSM_constant . } . ' \
            'OPTIONAL { ?parentSM <' + constants.RML_REFERENCE + '> ?parentSM_reference . } . ' \
            '?parentSM <' + constants.R2RML_TERM_TYPE + '> ?parentSM_termtype . ' \
            'OPTIONAL { ?childTM <' + constants.RML_LOGICAL_SOURCE + '> ?child_logical_source . ' \
                      ' ?parentTM <' + constants.RML_LOGICAL_SOURCE + '> ?parent_logical_source . ' \
                      ' FILTER ( ?child_logical_source != ?parent_logical_source) } . ' \
            'OPTIONAL { ?childTM <' + constants.RML_QUERY + '> ?child_query . ' \
                      ' ?parentTM <' + constants.RML_QUERY + '> ?parent_query . ' \
                      ' FILTER ( ?child_query != ?parent_query ) } . ' \
            'OPTIONAL { ?childTM <' + constants.RML_ITERATOR + '> ?child_iterator . ' \
                      ' ?parentTM <' + constants.RML_ITERATOR + '> ?parent_iterator . ' \
                      ' FILTER ( ?child_query != ?parent_query ) } . ' \
            'OPTIONAL { ?childTM <' + constants.R2RML_TABLE_NAME + '> ?child_tablename . ' \
                      ' ?parentTM <' + constants.R2RML_TABLE_NAME + '> ?parent_tablename . ' \
                      ' FILTER ( ?child_query != ?parent_query ) } . }'

    for OM, join_condition, parentSM_template, parentSM_constant, parentSM_reference, parentSM_termtype in \
            mapping_graph.query(query):
        mapping_graph.remove((OM, None, None))
        if join_condition:
            mapping_graph.remove((join_condition, None, None))

        mapping_graph.add((OM, rdflib.term.URIRef(constants.R2RML_TERM_TYPE), parentSM_termtype))
        if parentSM_template:
            mapping_graph.add((OM, rdflib.term.URIRef(constants.R2RML_TEMPLATE), parentSM_template))
        elif parentSM_constant:
            mapping_graph.add((OM, rdflib.term.URIRef(constants.R2RML_CONSTANT), parentSM_constant))
        elif parentSM_reference:
            mapping_graph.add((OM, rdflib.term.URIRef(constants.RML_REFERENCE), parentSM_reference))

    return mapping_graph


def _get_join_object_maps_join_conditions(join_query_results):
    """
    Creates a dictionary with the results of the JOIN_CONDITION_PARSING_QUERY. The keys are the identifiers of the
    child triples maps of the join condition. The values of the dictionary are in turn other dictionaries with two
    items, child_value and parent_value, representing a join condition.
    """

    join_conditions_dict = {}

    for join_condition in join_query_results:
        # add the child triples map identifier if it is not in the dictionary
        if join_condition.object_map not in join_conditions_dict:
            join_conditions_dict[join_condition.object_map] = {}

        # add the new join condition (note that several join conditions can apply in a join)
        join_conditions_dict[join_condition.object_map][str(join_condition.join_condition)] = \
            {'child_value': str(join_condition.child_value), 'parent_value': str(join_condition.parent_value)}

    return join_conditions_dict


def _validate_no_repeated_triples_maps(mapping_graph, source_name):
    """
    Checks that there are no repeated triples maps in the mapping rules of a source. This is important because
    if there are repeated triples maps (i.e. triples map with the same identifier), it is not possible to process
    parent triples maps correctly.
    """

    query = 'SELECT ?triples_map_id WHERE { ?triples_map_id <http://www.w3.org/ns/r2rml#subjectMap> ?_subject_map . }'

    # get the identifiers of all the triples maps in the graph
    triples_map_ids = [str(result.triples_map_id) for result in list(mapping_graph.query(query))]

    # get the identifiers that are repeated
    repeated_triples_map_ids = utils.get_repeated_elements_in_list(triples_map_ids)

    # if there are any repeated identifiers, then it will produce errors during materialization
    if len(repeated_triples_map_ids) > 0:
        raise Exception("The following triples maps in data source `" + source_name + "` are repeated: " +
                        str(repeated_triples_map_ids) + '.')


def _transform_mappings_into_dataframe(mapping_query_results, join_query_results, section_name):
    """
    Builds a Pandas DataFrame from the results obtained from MAPPING_PARSING_QUERY and
    JOIN_CONDITION_PARSING_QUERY for one source.
    """

    # mapping rules in graph to DataFrame
    source_mappings_df = pd.DataFrame(mapping_query_results.bindings)
    source_mappings_df.columns = source_mappings_df.columns.map(str)

    # process mapping rules with joins
    # create a dict with child triples maps in the keys and its join conditions in the values
    join_conditions_dict = _get_join_object_maps_join_conditions(join_query_results)
    # map the dict with the join conditions to the mapping rules in the DataFrame
    source_mappings_df['join_conditions'] = source_mappings_df['object_map'].map(join_conditions_dict)
    # needed for later hashing the dataframe
    source_mappings_df['join_conditions'] = source_mappings_df['join_conditions'].where(
        pd.notna(source_mappings_df['join_conditions']), '')
    # convert the join condition dicts to string (can later be converted back to dict)
    source_mappings_df['join_conditions'] = source_mappings_df['join_conditions'].astype(str)
    # object_map column no longer needed, remove it
    source_mappings_df = source_mappings_df.drop('object_map', axis=1)

    # link the mapping rules to their data source name
    source_mappings_df['source_name'] = section_name

    return source_mappings_df


def _is_delimited_identifier(identifier):
    """
    Checks if an identifier is delimited or not.
    """

    if len(identifier) > 2:
        if identifier[0] == '"' and identifier[len(identifier) - 1] == '"':
            return True
    return False


def _get_undelimited_identifier(identifier):
    """
    Removes delimiters from the identifier if it is delimited.
    """

    if pd.notna(identifier):
        identifier = str(identifier)
        if _is_delimited_identifier(identifier):
            return identifier[1:-1]
    return identifier


def _get_valid_template_identifiers(template):
    """
    Removes delimiters from delimited identifiers in a template.
    """

    if pd.notna(template):
        return template.replace('{"', '{').replace('"}', '}')
    return template


class MappingParser:

    def __init__(self, config):
        self.mappings_df = pd.DataFrame(columns=MAPPINGS_DATAFRAME_COLUMNS)
        self.config = config

    def __str__(self):
        return str(self.mappings_df)

    def __repr__(self):
        return repr(self.mappings_df)

    def __len__(self):
        return len(self.mappings_df)

    def parse_mappings(self):
        self._get_from_r2_rml()
        self._normalize_mappings()
        self._infer_datatypes()

        self.validate_mappings()

        logging.info(str(len(self.mappings_df)) + ' mapping rules retrieved.')

        # generate mapping partitions
        mapping_partitioner = MappingPartitioner(self.mappings_df, self.config)
        self.mappings_df = mapping_partitioner.partition_mappings()

        # replace empty strings with NaN
        self.mappings_df = self.mappings_df.replace(r'^\s*$', np.nan, regex=True)

        return self.mappings_df

    def _get_from_r2_rml(self):
        """
        Parses the mapping files of all data sources in the config file and adds the parsed mappings rules to a
        common DataFrame for all data sources. If parallelization is enabled and multiple data sources are provided,
        each mapping file is parsed in parallel.
        """

        if self.config.is_multiprocessing_enabled() and self.config.has_multiple_data_sources():
            pool = mp.Pool(self.config.get_number_of_processes())
            mappings_dfs = pool.map(self._parse_data_source_mapping_files, self.config.get_data_sources_sections())
            self.mappings_df = pd.concat([self.mappings_df, pd.concat(mappings_dfs)])
        else:
            for section_name in self.config.get_data_sources_sections():
                data_source_mappings_df = self._parse_data_source_mapping_files(section_name)
                self.mappings_df = pd.concat([self.mappings_df, data_source_mappings_df])

        self.mappings_df = self.mappings_df.reset_index(drop=True)

    def _parse_data_source_mapping_files(self, section_name):
        """
        Creates a Pandas DataFrame with the mapping rules of a data source. It loads the mapping files in an rdflib
        graph and recognizes the mapping language used. Mappings are translated to RML.
        It performs queries MAPPING_PARSING_QUERY and JOIN_CONDITION_PARSING_QUERY and process the results to build a
        DataFrame with the mapping rules. Also verifies that there are not repeated triples maps in the mappings.
        """

        # create an empty graph
        mapping_graph = rdflib.Graph()

        mapping_file_paths = self.config.get_mappings_files(section_name)
        try:
            # load mapping rules to the graph
            [mapping_graph.parse(f, format=rdflib.util.guess_format(f)) for f in mapping_file_paths]
        except Exception as n3_mapping_parse_exception:
            raise Exception(n3_mapping_parse_exception)

        # convert R2RML rules to RML, so that we can assume RML for parsing
        mapping_graph = _mapping_to_rml(mapping_graph)
        # convert rr:class to new POMs
        mapping_graph = _rdf_class_to_pom(mapping_graph)
        # expand constant shortcut properties rr:subject, rr:predicate, rr:object and rr:graph
        mapping_graph = _expand_constant_shortcut_properties(mapping_graph)
        # move graph maps in subject maps to the predicate object maps of subject maps
        mapping_graph = _subject_graph_maps_to_pom(mapping_graph)
        # complete predicate object maps without graph maps with rr:defaultGraph
        mapping_graph = _complete_pom_with_default_graph(mapping_graph)
        # if a term as no associated rr:termType, complete it according to R2RML specification
        mapping_graph = _complete_termtypes(mapping_graph)
        # remove self joins
        mapping_graph = _remove_self_joins(mapping_graph)
        # add rdf:type RML classes
        mapping_graph = _complete_rml_classes(mapping_graph)

        # parse the mappings with the parsing queries
        mapping_query_results = mapping_graph.query(MAPPING_PARSING_QUERY)
        join_query_results = mapping_graph.query(JOIN_CONDITION_PARSING_QUERY)

        # check triples maps are not repeated, which would lead to errors (because of repeated triples maps identifiers)
        _validate_no_repeated_triples_maps(mapping_graph, section_name)

        # convert the SPARQL result set with the parsed mappings to DataFrame
        return _transform_mappings_into_dataframe(mapping_query_results, join_query_results, section_name)

    def _normalize_mappings(self):
        # start by removing duplicated triples
        self.mappings_df = self.mappings_df.drop_duplicates()

        # complete source type with reference formulation
        self._complete_source_types()

        # ignore the delimited identifiers (this is not conformant with R2MRL specification)
        self._remove_delimiters_from_mappings()

        # remove mapping rules with no predicate or object (subject map is conserved because rdf class was added as POM)
        self.mappings_df = self.mappings_df.dropna(subset=['predicate_constant', 'predicate_template',
                                                           'predicate_reference', 'object_constant', 'object_template',
                                                           'object_reference'], how='all')

        # create a unique id for each mapping rule
        self.mappings_df.insert(0, 'id', self.mappings_df.reset_index(drop=True).index)

    def _complete_source_types(self):
        """
        Adds a column with the source type. The source type is taken from the value provided in the config for that data
        source. If it is not provided, it is taken from the reference formulation in the mapping rule.
        """

        for i, mapping_rule in self.mappings_df.iterrows():
            if self.config.has_source_type(mapping_rule['source_name']):
                # take the source type from the config if it is provided
                self.mappings_df.at[i, 'source_type'] = self.config.get_source_type(mapping_rule['source_name']).upper()
            elif pd.notna(mapping_rule['ref_form']):
                # take the source type from the reference formulation (fragment) in the mapping rules
                self.mappings_df.at[i, 'source_type'] = str(mapping_rule['ref_form']).split('#')[-1].upper()
            else:
                logging.error('No source type could be retrieved for mapping rule some mapping rules.')

        # ref form is no longer needed, remove it
        self.mappings_df = self.mappings_df.drop('ref_form', axis=1)

    def _remove_delimiters_from_mappings(self):
        """
        Removes delimiters from all identifiers in the mapping rules in the input DataFrame.
        """

        for i, mapping_rule in self.mappings_df.iterrows():
            self.mappings_df.at[i, 'tablename'] = _get_undelimited_identifier(mapping_rule['tablename'])
            self.mappings_df.at[i, 'subject_template'] = _get_valid_template_identifiers(
                mapping_rule['subject_template'])
            self.mappings_df.at[i, 'subject_reference'] = _get_undelimited_identifier(
                mapping_rule['subject_reference'])
            self.mappings_df.at[i, 'graph_reference'] = _get_undelimited_identifier(
                mapping_rule['graph_reference'])
            self.mappings_df.at[i, 'graph_template'] = _get_valid_template_identifiers(
                mapping_rule['graph_template'])
            self.mappings_df.at[i, 'predicate_template'] = _get_valid_template_identifiers(
                mapping_rule['predicate_template'])
            self.mappings_df.at[i, 'predicate_reference'] = _get_undelimited_identifier(
                mapping_rule['predicate_reference'])
            self.mappings_df.at[i, 'object_template'] = _get_valid_template_identifiers(
                mapping_rule['object_template'])
            self.mappings_df.at[i, 'object_reference'] = _get_undelimited_identifier(
                mapping_rule['object_reference'])

            # if join_condition is not null and it is not empty
            if pd.notna(mapping_rule['join_conditions']) and mapping_rule['join_conditions']:
                join_conditions = eval(mapping_rule['join_conditions'])
                for key, value in join_conditions.items():
                    join_conditions[key]['child_value'] = _get_undelimited_identifier(
                        join_conditions[key]['child_value'])
                    join_conditions[key]['parent_value'] = _get_undelimited_identifier(
                        join_conditions[key]['parent_value'])
                    self.mappings_df.at[i, 'join_conditions'] = str(join_conditions)

    def _infer_datatypes(self):
        """
        Get RDF datatypes for rules corresponding to relational data sources if they are not overridden in the mapping
        rules. The inferring of RDF datatypes is defined in R2RML specification
        (https://www.w3.org/2001/sw/rdb2rdf/r2rml/#natural-mapping).
        """

        # return if datatype inferring is not enabled in the config
        if not self.config.infer_sql_datatypes():
            return

        for i, mapping_rule in self.mappings_df.iterrows():
            # datatype inferring only applies to relational data sources
            if (mapping_rule['source_type'] == constants.RDB_SOURCE_TYPE) and (
                    # datatype inferring only applies to literals
                    mapping_rule['object_termtype'] == constants.R2RML_LITERAL) and (
                    # if the literal has a language tag or an overridden datatype, datatype inference does not apply
                    pd.isna(mapping_rule['object_datatype']) and pd.isna(mapping_rule['object_language'])):

                if pd.notna(mapping_rule['tablename']) and pd.notna(mapping_rule['object_reference']):
                    inferred_data_type = relational_source.get_column_datatype(
                        self.config, mapping_rule['source_name'], mapping_rule['tablename'],
                        mapping_rule['object_reference']
                    )

                    self.mappings_df.at[i, 'object_datatype'] = inferred_data_type
                    if inferred_data_type:
                        logging.debug("`" + inferred_data_type + "` datatype inferred for column `" +
                                      mapping_rule['object_reference'] + "` of table `" +
                                      mapping_rule['tablename'] + "` in data source `" +
                                      mapping_rule['source_name'] + "`.")

                elif pd.notna(mapping_rule['query']):
                    # if mapping rule has a query, get the table names in the query
                    table_names = sql_metadata.get_query_tables(mapping_rule['query'])
                    for table_name in table_names:
                        # for each table in the query get the datatype of the object reference in that table if an
                        # exception is thrown, then the reference is not a column in that table, and nothing is done
                        try:
                            data_type = relational_source.get_column_datatype(
                                self.config, mapping_rule['source_name'], table_name,
                                mapping_rule['object_reference']
                            )

                            self.mappings_df.at[i, 'object_datatype'] = data_type
                            if data_type:
                                logging.debug("`" + data_type + "` datatype inferred for reference `" +
                                              mapping_rule['object_reference'] + "` in query [" +
                                              mapping_rule['query'] + "] in data source `" +
                                              mapping_rule['source_name'] + "`.")

                            # already found it, end looping
                            break
                        except:
                            pass

    def validate_mappings(self):
        """
        Checks that the mapping rules in the input DataFrame are valid. If something is wrong in the mappings the
        execution is stopped. Specifically it is checked that termtypes are correct, and that language tags and
        datatypes are used properly. Also checks that different data sources do not have triples map with the same id.
        """

        # check termtypes are correct (i.e. that they are rr:IRI, rr:BlankNode or rr:Literal and that subject map is
        # not a rr:literal). Use subset operation
        subject_termtypes = set([str(termtype) for termtype in set(self.mappings_df['subject_termtype'])])
        if not (subject_termtypes <= {constants.R2RML_IRI, constants.R2RML_BLANK_NODE}):
            raise ValueError('Found an invalid subject termtype. Found values ' + str(subject_termtypes) + \
                             '. Subject maps must be ' + constants.R2RML_IRI + ' or ' + constants.R2RML_BLANK_NODE + \
                             '.')

        object_termtypes = set([str(termtype) for termtype in set(self.mappings_df['object_termtype'])])
        if not (object_termtypes <= {constants.R2RML_IRI, constants.R2RML_BLANK_NODE, constants.R2RML_LITERAL}):
            raise ValueError('Found an invalid object termtype. Found values ' + str(object_termtypes) + \
                             '. Object maps must be ' + constants.R2RML_IRI + ', ' + constants.R2RML_BLANK_NODE + \
                             ' or ' + constants.R2RML_LITERAL + '.')

        # if there is a datatype or language tag then the object map termtype must be a rr:Literal
        if len(self.mappings_df.loc[(self.mappings_df['object_termtype'] != constants.R2RML_LITERAL) &
                                    pd.notna(self.mappings_df['object_datatype']) &
                                    pd.notna(self.mappings_df['object_language'])]) > 0:
            raise Exception('Found object maps with a language tag or a datatype, '
                            'but that do not have termtype rr:Literal.')

        # language tags and datatypes cannot be used simultaneously, language tags are used if both are given
        if len(self.mappings_df.loc[pd.notna(self.mappings_df['object_language']) &
                                    pd.notna(self.mappings_df['object_datatype'])]) > 0:
            logging.warning('Found object maps with a language tag and a datatype. Both of them cannot be used '
                            'simultaneously for the same object map, and the language tag has preference.')

        # check that a triples map id is not repeated in different data sources
        # Get unique source names and triples map identifiers
        aux_mappings_df = self.mappings_df[['source_name', 'triples_map_id']].drop_duplicates()
        # get repeated triples map identifiers
        repeated_triples_map_ids = utils.get_repeated_elements_in_list(
            list(aux_mappings_df['triples_map_id'].astype(str)))
        # of those repeated identifiers
        repeated_triples_map_ids = [tm_id for tm_id in repeated_triples_map_ids]
        if len(repeated_triples_map_ids) > 0:
            raise Exception('The following triples maps appear in more than one data source: ' +
                            str(repeated_triples_map_ids) +
                            '. Check the mapping files, one triple map cannot be repeated in different data sources.')
