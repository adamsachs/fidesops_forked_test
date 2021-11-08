import copy
import logging
import random
from datetime import datetime
from unittest import mock
from unittest.mock import Mock

import dask
import pytest

from fidesops.common_exceptions import InsufficientDataException
from fidesops.core.config import config
from fidesops.graph.config import FieldAddress
from fidesops.graph.graph import DatasetGraph, Edge, Node
from fidesops.graph.traversal import TraversalNode
from fidesops.models.datasetconfig import convert_dataset_to_graph
from fidesops.models.policy import Policy
from fidesops.models.policy import Rule, RuleTarget, ActionType
from fidesops.models.privacy_request import ExecutionLog, PrivacyRequest
from fidesops.schemas.dataset import FidesopsDataset
from fidesops.service.connectors import get_connector
from fidesops.task import graph_task
from fidesops.task.graph_task import filter_data_categories
from ..graph.graph_test_util import (
    assert_rows_match,
    records_matching_fields,
    field,
    erasure_policy,
)
from ..task.traversal_data import integration_db_graph, integration_db_dataset

dask.config.set(scheduler="processes")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
sample_postgres_configuration_policy = erasure_policy(
    "system.operations",
    "user.derived.identifiable.unique_id",
    "user.derived.nonidentifiable.sensor",
    "user.provided.identifiable.contact.city",
    "user.provided.identifiable.contact.email",
    "user.provided.identifiable.contact.postal_code",
    "user.provided.identifiable.contact.state",
    "user.provided.identifiable.contact.street",
    "user.provided.identifiable.financial.account_number",
    "user.provided.identifiable.financial",
    "user.provided.identifiable.name",
    "user.provided.nonidentifiable",
)


@pytest.mark.integration
def test_sql_erasure_ignores_collections_without_pk(
    db, postgres_inserts, integration_postgres_config
):
    seed_email = postgres_inserts["customer"][0]["email"]
    policy = erasure_policy(
        "A", "B"
    )  # makes an erasure policy with two data categories to match against
    dataset = integration_db_dataset("postgres_example", "postgres_example")

    field([dataset], ("postgres_example", "address", "id")).primary_key = False

    # set categories: A,B will be marked erasable, C will not
    field([dataset], ("postgres_example", "address", "city")).data_categories = ["A"]
    field([dataset], ("postgres_example", "address", "state")).data_categories = ["B"]
    field([dataset], ("postgres_example", "address", "zip")).data_categories = ["C"]
    field([dataset], ("postgres_example", "customer", "name")).data_categories = ["A"]

    graph = DatasetGraph(dataset)
    privacy_request = PrivacyRequest(
        id=f"test_sql_erasure_task_{random.randint(0, 1000)}"
    )
    access_request_data = graph_task.run_access_request(
        privacy_request,
        policy,
        graph,
        [integration_postgres_config],
        {"email": seed_email},
    )
    v = graph_task.run_erasure(
        privacy_request,
        policy,
        graph,
        [integration_postgres_config],
        {"email": seed_email},
        access_request_data,
    )

    logs = (
        ExecutionLog.query(db=db)
        .filter(ExecutionLog.privacy_request_id == privacy_request.id)
        .all()
    )
    logs = [log.__dict__ for log in logs]
    # since address has no primary_key=True field, it's erasure is skipped
    assert (
        len(
            records_matching_fields(
                logs,
                dataset_name="postgres_example",
                collection_name="address",
                message="No values were erased since no primary key was defined for this collection",
            )
        )
        == 1
    )
    assert v == {
        "postgres_example:customer": 1,
        "postgres_example:payment_card": 0,
        "postgres_example:orders": 0,
        "postgres_example:address": 0,
    }


@pytest.mark.integration
def test_sql_erasure_task(db, postgres_inserts, integration_postgres_config):
    seed_email = postgres_inserts["customer"][0]["email"]

    policy = erasure_policy("A", "B")
    dataset = integration_db_dataset("postgres_example", "postgres_example")
    field([dataset], ("postgres_example", "address", "id")).primary_key = True
    # set categories: A,B will be marked erasable, C will not
    field([dataset], ("postgres_example", "address", "city")).data_categories = ["A"]
    field([dataset], ("postgres_example", "address", "state")).data_categories = ["B"]
    field([dataset], ("postgres_example", "address", "zip")).data_categories = ["C"]
    field([dataset], ("postgres_example", "customer", "name")).data_categories = ["A"]
    graph = DatasetGraph(dataset)
    privacy_request = PrivacyRequest(
        id=f"test_sql_erasure_task_{random.randint(0, 1000)}"
    )
    access_request_data = graph_task.run_access_request(
        privacy_request,
        policy,
        graph,
        [integration_postgres_config],
        {"email": seed_email},
    )
    v = graph_task.run_erasure(
        privacy_request,
        policy,
        graph,
        [integration_postgres_config],
        {"email": seed_email},
        access_request_data,
    )

    assert v == {
        "postgres_example:customer": 1,
        "postgres_example:payment_card": 0,
        "postgres_example:orders": 0,
        "postgres_example:address": 2,
    }


@pytest.mark.integration
def test_sql_access_request_task(db, policy, integration_postgres_config) -> None:

    privacy_request = PrivacyRequest(id=f"test_sql_access_request_task_{random.randint(0, 1000)}")

    v = graph_task.run_access_request(
        privacy_request,
        policy,
        integration_db_graph("postgres_example"),
        [integration_postgres_config],
        {"email": "customer-1@example.com"},
    )

    assert_rows_match(
        v["postgres_example:address"],
        min_size=2,
        keys=["id", "street", "city", "state", "zip"],
    )
    assert_rows_match(
        v["postgres_example:orders"],
        min_size=3,
        keys=["id", "customer_id", "shipping_address_id", "payment_card_id"],
    )
    assert_rows_match(
        v["postgres_example:payment_card"],
        min_size=2,
        keys=["id", "name", "ccn", "customer_id", "billing_address_id"],
    )
    assert_rows_match(
        v["postgres_example:customer"],
        min_size=1,
        keys=["id", "name", "email", "address_id"],
    )

    # links
    assert v["postgres_example:customer"][0]["email"] == "customer-1@example.com"

    logs = (
        ExecutionLog.query(db=db)
        .filter(ExecutionLog.privacy_request_id == privacy_request.id)
        .all()
    )

    logs = [log.__dict__ for log in logs]
    assert (
        len(
            records_matching_fields(
                logs, dataset_name="postgres_example", collection_name="customer"
            )
        )
        > 0
    )
    assert (
        len(
            records_matching_fields(
                logs, dataset_name="postgres_example", collection_name="address"
            )
        )
        > 0
    )
    assert (
        len(
            records_matching_fields(
                logs, dataset_name="postgres_example", collection_name="orders"
            )
        )
        > 0
    )
    assert (
        len(
            records_matching_fields(
                logs,
                dataset_name="postgres_example",
                collection_name="payment_card",
            )
        )
        > 0
    )


@pytest.mark.integration
def test_filter_on_data_categories(
    db,
    privacy_request,
    connection_config,
    example_datasets,
    policy,
    integration_postgres_config,
):
    postgres_config = copy.copy(integration_postgres_config)

    rule = Rule.create(
        db=db,
        data={
            "action_type": ActionType.access.value,
            "client_id": policy.client_id,
            "name": "Valid Access Rule",
            "policy_id": policy.id,
            "storage_destination_id": policy.rules[0].storage_destination.id,
        },
    )

    rule_target = RuleTarget.create(
        db,
        data={
            "name": "Test Rule 1",
            "key": "test-rule-1",
            "data_category": "user.provided.identifiable.contact.street",
            "rule_id": rule.id,
        },
    )

    dataset = FidesopsDataset(**example_datasets[0])
    graph = convert_dataset_to_graph(dataset, integration_postgres_config.key)
    dataset_graph = DatasetGraph(*[graph])

    access_request_results = graph_task.run_access_request(
        privacy_request,
        policy,
        dataset_graph,
        [postgres_config],
        {"email": "customer-1@example.com"},
    )

    target_categories = {target.data_category for target in rule.targets}
    filtered_results = filter_data_categories(
        access_request_results, target_categories, dataset_graph
    )

    # One rule target, with data category that maps to house/street on address collection only.
    assert filtered_results == {
        "postgres_example_test_dataset:address": [
            {"house": 123, "street": "Example Street"},
            {"house": 4, "street": "Example Lane"},
        ]
    }

    # Specify the target category:
    target_categories = {"user.provided.identifiable.contact"}
    filtered_results = filter_data_categories(
        access_request_results, target_categories, dataset_graph
    )

    assert filtered_results == {
        "postgres_example_test_dataset:visit": [{"email": "customer-1@example.com"}],
        "postgres_example_test_dataset:address": [
            {
                "city": "Exampletown",
                "house": 123,
                "state": "NY",
                "street": "Example Street",
                "zip": "12345",
            },
            {
                "city": "Exampletown",
                "house": 4,
                "state": "NY",
                "street": "Example Lane",
                "zip": "12321",
            },
        ],
        "postgres_example_test_dataset:service_request": [
            {"alt_email": "customer-1-alt@example.com"}
        ],
        "postgres_example_test_dataset:customer": [{"email": "customer-1@example.com"}],
    }

    # Add two more rule targets, one that is also applicable to the address table, and
    # another that spans multiple tables.

    rule_target_two = RuleTarget.create(
        db,
        data={
            "name": "Test Rule 2",
            "key": "test-rule-2",
            "data_category": "user.provided.identifiable.contact.email",
            "rule_id": rule.id,
        },
    )

    rule_target_three = RuleTarget.create(
        db,
        data={
            "name": "Test Rule 3",
            "key": "test-rule-3",
            "data_category": "user.provided.identifiable.contact.state",
            "rule_id": rule.id,
        },
    )

    target_categories = {target.data_category for target in rule.targets}
    filtered_results = filter_data_categories(
        access_request_results, target_categories, dataset_graph
    )
    assert filtered_results == {
        "postgres_example_test_dataset:service_request": [
            {"alt_email": "customer-1-alt@example.com"}
        ],
        "postgres_example_test_dataset:address": [
            {"house": 123, "state": "NY", "street": "Example Street"},
            {"house": 4, "state": "NY", "street": "Example Lane"},
        ],
        "postgres_example_test_dataset:visit": [{"email": "customer-1@example.com"}],
        "postgres_example_test_dataset:customer": [{"email": "customer-1@example.com"}],
    }

    rule_target.delete(db)
    rule_target_two.delete(db)
    rule_target_three.delete(db)
    rule_target.delete(db)


class TestRetrievingData:
    @pytest.fixture
    def connector(self, integration_postgres_config):

        return get_connector(integration_postgres_config)

    @pytest.fixture
    def traversal_node(self, example_datasets, integration_postgres_config):
        dataset = FidesopsDataset(**example_datasets[0])
        graph = convert_dataset_to_graph(dataset, integration_postgres_config.key)
        node = Node(graph, graph.collections[1])  # customer collection
        traversal_node = TraversalNode(node)
        return traversal_node

    @pytest.mark.integration
    @mock.patch("fidesops.graph.traversal.TraversalNode.incoming_edges")
    def test_retrieving_data(
        self,
        mock_incoming_edges: Mock,
        db,
        connector,
        traversal_node,
    ):
        mock_incoming_edges.return_value = {
            Edge(
                FieldAddress("fake_dataset", "fake_collection", "email"),
                FieldAddress("postgres_example_test_dataset", "customer", "email"),
            )
        }

        results = connector.retrieve_data(
            traversal_node, Policy(), {"email": ["customer-1@example.com"]}
        )
        assert len(results) is 1
        assert results == [
            {
                "address_id": 1,
                "created": datetime(2020, 4, 1, 11, 47, 42),
                "email": "customer-1@example.com",
                "id": 1,
                "name": "John Customer",
            }
        ]

    @pytest.mark.integration
    @mock.patch("fidesops.graph.traversal.TraversalNode.incoming_edges")
    def test_retrieving_data_no_input(
        self,
        mock_incoming_edges: Mock,
        db,
        connector,
        traversal_node,
    ):
        mock_incoming_edges.return_value = {
            Edge(
                FieldAddress("fake_dataset", "fake_collection", "email"),
                FieldAddress("postgres_example_test_dataset", "customer", "email"),
            )
        }

        assert [] == connector.retrieve_data(traversal_node, Policy(), {"email": []})

        assert [] == connector.retrieve_data(traversal_node, Policy(), {})

        assert [] == connector.retrieve_data(
            traversal_node, Policy(), {"bad_key": ["test"]}
        )

        assert [] == connector.retrieve_data(
            traversal_node, Policy(), {"email": [None]}
        )

        assert [] == connector.retrieve_data(traversal_node, Policy(), {"email": None})

    @pytest.mark.integration
    @mock.patch("fidesops.graph.traversal.TraversalNode.incoming_edges")
    def test_retrieving_data_input_not_in_table(
        self,
        mock_incoming_edges: Mock,
        db,
        privacy_request,
        connection_config,
        example_datasets,
        connector,
        traversal_node,
    ):
        mock_incoming_edges.return_value = {
            Edge(
                FieldAddress("fake_dataset", "fake_collection", "email"),
                FieldAddress("postgres_example_test_dataset", "customer", "email"),
            )
        }
        results = connector.retrieve_data(
            traversal_node, Policy(), {"email": ["customer_not_in_dataset@example.com"]}
        )
        assert results == []


class TestRetryIntegration:
    @pytest.mark.integration
    @mock.patch("fidesops.service.connectors.sql_connector.SQLConnector.retrieve_data")
    def test_retry_access_request(
        self,
        mock_retrieve,
        db,
        privacy_request,
        connection_config,
        example_datasets,
        policy,
        integration_postgres_config,
    ):
        config.execution.TASK_RETRY_COUNT = 1
        config.execution.TASK_RETRY_DELAY = 0.1
        config.execution.TASK_RETRY_BACKOFF = 0.01

        dataset = FidesopsDataset(**example_datasets[0])
        graph = convert_dataset_to_graph(dataset, integration_postgres_config.key)
        dataset_graph = DatasetGraph(*[graph])

        # Mock errors with retrieving data
        mock_retrieve.side_effect = Exception("Insufficient data")

        # Call run_access_request with an email that isn't in the database
        access_request_results = graph_task.run_access_request(
            privacy_request,
            sample_postgres_configuration_policy,
            dataset_graph,
            [integration_postgres_config],
            {"email": "customer-5@example.com"},
        )

        execution_logs = db.query(ExecutionLog).filter_by(
            privacy_request_id=privacy_request.id
        )

        assert 33 == execution_logs.count()

        processing = execution_logs.filter_by(status="in_processing")
        assert 11 == processing.count()
        assert {
            "employee",
            "visit",
            "customer",
            "report",
            "orders",
            "payment_card",
            "service_request",
            "login",
            "address",
            "order_item",
            "product",
        } == set([pro.collection_name for pro in processing])

        errored = execution_logs.filter_by(status="error")
        retried = execution_logs.filter_by(status="retrying")

        assert 11 == retried.count()
        assert 11 == errored.count()

        cannot_reach = {
            "visit",
            "payment_card",
            "customer",
            "orders",
            "order_item",
            "address",
            "product",
            "service_request",
            "login",
            "employee",
            "report",
        }

        assert cannot_reach == set([ret.collection_name for ret in retried])
        assert cannot_reach == set([err.collection_name for err in errored])

        complete = execution_logs.filter_by(status="complete")
        assert 0 == complete.count()

        # No results were accessible because all retrieve_data calls failed.
        assert access_request_results == {}

    @pytest.mark.integration
    @mock.patch("fidesops.service.connectors.sql_connector.SQLConnector.mask_data")
    def test_retry_erasure(
        self,
        mock_mask: Mock,
        db,
        privacy_request,
        connection_config,
        example_datasets,
        policy,
        integration_postgres_config,
    ):
        config.execution.TASK_RETRY_COUNT = 1
        config.execution.TASK_RETRY_DELAY = 0.1
        config.execution.TASK_RETRY_BACKOFF = 0.01

        dataset = FidesopsDataset(**example_datasets[0])
        graph = convert_dataset_to_graph(dataset, integration_postgres_config.key)
        dataset_graph = DatasetGraph(*[graph])

        # Mock errors with masking data
        mock_mask.side_effect = Exception("Insufficient data")

        # Call run_erasure with an email that isn't in the database
        erasure_results = graph_task.run_erasure(
            privacy_request,
            sample_postgres_configuration_policy,
            dataset_graph,
            [integration_postgres_config],
            {"email": "customer-5@example.com"},
            {
                "postgres_example_test_dataset:employee": [],
                "postgres_example_test_dataset:visit": [],
                "postgres_example_test_dataset:customer": [],
                "postgres_example_test_dataset:report": [],
                "postgres_example_test_dataset:orders": [],
                "postgres_example_test_dataset:payment_card": [],
                "postgres_example_test_dataset:service_request": [],
                "postgres_example_test_dataset:login": [],
                "postgres_example_test_dataset:address": [],
                "postgres_example_test_dataset:order_item": [],
                "postgres_example_test_dataset:product": [],
            },
        )

        execution_logs = db.query(ExecutionLog).filter_by(
            privacy_request_id=privacy_request.id
        )

        assert 31 == execution_logs.count()
        processing = execution_logs.filter_by(status="in_processing")
        assert 11 == processing.count()
        assert {
            "orders",
            "order_item",
            "visit",
            "payment_card",
            "customer",
            "employee",
            "address",
            "service_request",
            "login",
            "report",
            "product",
        } == set([pro.collection_name for pro in processing])

        errored = execution_logs.filter_by(status="error")
        retried = execution_logs.filter_by(status="retrying")
        assert 9 == retried.count()
        assert 9 == errored.count()

        complete = execution_logs.filter_by(status="complete")
        assert 2 == complete.count()

        assert erasure_results == {
            "postgres_example_test_dataset:visit": 0,
            "postgres_example_test_dataset:service_request": 0,
            "postgres_example_test_dataset:employee": 0,
            "postgres_example_test_dataset:customer": 0,
            "postgres_example_test_dataset:report": 0,
            "postgres_example_test_dataset:address": 0,
            "postgres_example_test_dataset:payment_card": 0,
            "postgres_example_test_dataset:orders": 0,
            "postgres_example_test_dataset:login": 0,
            "postgres_example_test_dataset:order_item": 0,
            "postgres_example_test_dataset:product": 0,
        }