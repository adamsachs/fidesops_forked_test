import logging
from collections import defaultdict
from datetime import date
from typing import List, Optional, Union, DefaultDict, Dict

from fastapi import APIRouter, Body, Depends, Security, HTTPException
from fastapi_pagination import Page, Params
from fastapi_pagination.bases import AbstractPage
from fastapi_pagination.ext.sqlalchemy import paginate
from pydantic import conlist
from sqlalchemy.orm import Session
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
    HTTP_424_FAILED_DEPENDENCY,
)

from fidesops import common_exceptions
from fidesops.api import deps
from fidesops.api.v1 import scope_registry as scopes
from fidesops.api.v1 import urn_registry as urls
from fidesops.api.v1.scope_registry import PRIVACY_REQUEST_READ
from fidesops.api.v1.urn_registry import REQUEST_PREVIEW
from fidesops.common_exceptions import TraversalError
from fidesops.db.session import get_db_session
from fidesops.graph.config import CollectionAddress
from fidesops.graph.graph import DatasetGraph
from fidesops.graph.traversal import Traversal
from fidesops.models.client import ClientDetail
from fidesops.models.connectionconfig import ConnectionConfig
from fidesops.models.datasetconfig import DatasetConfig
from fidesops.models.policy import Policy
from fidesops.models.privacy_request import (
    ExecutionLog,
    PrivacyRequest,
    PrivacyRequestRunner,
    PrivacyRequestStatus,
)
from fidesops.schemas.dataset import DryRunDatasetResponse, CollectionAddressResponse
from fidesops.schemas.privacy_request import (
    PrivacyRequestCreate,
    PrivacyRequestResponse,
    PrivacyRequestVerboseResponse,
    ExecutionLogDetailResponse,
    BulkPostPrivacyRequests,
)
from fidesops.task.graph_task import collect_queries, EMPTY_REQUEST
from fidesops.task.task_resources import TaskResources
from fidesops.util.cache import FidesopsRedis
from fidesops.util.oauth_util import verify_oauth_client

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Privacy Requests"], prefix=urls.V1_URL_PREFIX)

EMBEDDED_EXECUTION_LOG_LIMIT = 50


@router.post(
    urls.PRIVACY_REQUESTS,
    status_code=200,
    response_model=BulkPostPrivacyRequests,
)
def create_privacy_request(
    *,
    cache: FidesopsRedis = Depends(deps.get_cache),
    db: Session = Depends(deps.get_db),
    client: ClientDetail = Security(
        verify_oauth_client, scopes=[scopes.PRIVACY_REQUEST_CREATE]
    ),
    data: conlist(PrivacyRequestCreate, max_items=50) = Body(...),  # type: ignore
) -> BulkPostPrivacyRequests:
    """
    Given a list of privacy request data elements, create corresponding PrivacyRequest objects
    or report failure and execute them within the FidesOps system.

    You cannot update privacy requests after they've been created.
    """
    created = []
    failed = []
    # Optional fields to validate here are those that are both nullable in the DB, and exist
    # on the Pydantic schema

    logger.info(f"Starting creation for {len(data)} privacy requests")

    optional_fields = ["external_id", "started_processing_at", "finished_processing_at"]
    for privacy_request_data in data:
        if len(privacy_request_data.identities) == 0:
            logger.warning(
                "Create failed for privacy request with no identities provided"
            )
            failure = {
                "message": "You must provide at least one identity to process",
                "data": privacy_request_data,
            }
            failed.append(failure)
            continue

        logger.info(f"Finding policy with key '{privacy_request_data.policy_key}'")
        policy = Policy.get_by(
            db=db,
            field="key",
            value=privacy_request_data.policy_key,
        )
        if policy is None:
            logger.warning(
                f"Create failed for privacy request with invalid policy key {privacy_request_data.policy_key}'"
            )

            failure = {
                "message": f"Policy with key {privacy_request_data.policy_key} does not exist",
                "data": privacy_request_data,
            }
            failed.append(failure)
            continue

        kwargs = {
            "requested_at": privacy_request_data.requested_at,
            "policy_id": policy.id,
            "status": "pending",
            "client_id": client.id,
        }
        for field in optional_fields:
            attr = getattr(privacy_request_data, field)
            if attr is not None:
                kwargs[field] = attr

        try:
            privacy_request = PrivacyRequest.create(db=db, data=kwargs)

            # Store identity in the cache
            logger.info(f"Caching identities for privacy request {privacy_request.id}")
            for identity in privacy_request_data.identities:
                privacy_request.cache_identity(identity)

            PrivacyRequestRunner(
                cache=cache,
                db=db,
                privacy_request=privacy_request,
            ).run()
        except common_exceptions.RedisConnectionError as exc:
            logger.error(exc)
            # Thrown when cache.ping() fails on cache connection retrieval
            raise HTTPException(
                status_code=HTTP_424_FAILED_DEPENDENCY,
                detail=exc.args[0],
            )
        except Exception as exc:
            logger.error(exc)
            failure = {
                "message": "This record could not be added",
                "data": kwargs,
            }
            failed.append(failure)
        else:
            created.append(privacy_request)

    return BulkPostPrivacyRequests(
        succeeded=created,
        failed=failed,
    )


@router.get(
    urls.PRIVACY_REQUESTS,
    dependencies=[Security(verify_oauth_client, scopes=[scopes.PRIVACY_REQUEST_READ])],
    response_model=Page[Union[PrivacyRequestVerboseResponse, PrivacyRequestResponse]],
)
def get_request_status(
    *,
    db: Session = Depends(deps.get_db),
    params: Params = Depends(),
    id: Optional[str] = None,
    status: Optional[PrivacyRequestStatus] = None,
    created_lt: Optional[date] = None,
    created_gt: Optional[date] = None,
    started_lt: Optional[date] = None,
    started_gt: Optional[date] = None,
    completed_lt: Optional[date] = None,
    completed_gt: Optional[date] = None,
    errored_lt: Optional[date] = None,
    errored_gt: Optional[date] = None,
    external_id: Optional[str] = None,
    verbose: Optional[bool] = False,
) -> AbstractPage[PrivacyRequest]:
    """Returns PrivacyRequest information. Supports a variety of optional query params.

    To fetch a single privacy request, use the id query param `?id=`.
    To see individual execution logs, use the verbose query param `?verbose=True`.
    """

    if any([completed_lt, completed_gt]) and any([errored_lt, errored_gt]):
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail="Cannot specify both succeeded and failed query params.",
        )

    logger.info(f"Finding all request statuses with pagination params {params}")

    query = db.query(PrivacyRequest)

    # Further restrict all PrivacyRequests by query params
    if id:
        query = query.filter(PrivacyRequest.id == id)
    if external_id:
        query = query.filter(PrivacyRequest.external_id == external_id)
    if status:
        query = query.filter(PrivacyRequest.status == status)
    if created_lt:
        query = query.filter(PrivacyRequest.created_at < created_lt)
    if created_gt:
        query = query.filter(PrivacyRequest.created_at > created_gt)
    if started_lt:
        query = query.filter(PrivacyRequest.started_processing_at < started_lt)
    if started_gt:
        query = query.filter(PrivacyRequest.started_processing_at > started_gt)
    if completed_lt:
        query = query.filter(
            PrivacyRequest.status == PrivacyRequestStatus.complete.value,
            PrivacyRequest.finished_processing_at < completed_lt,
        )
    if completed_gt:
        query = query.filter(
            PrivacyRequest.status == PrivacyRequestStatus.complete.value,
            PrivacyRequest.finished_processing_at > completed_gt,
        )
    if errored_lt:
        query = query.filter(
            PrivacyRequest.status == PrivacyRequestStatus.error.value,
            PrivacyRequest.finished_processing_at < errored_lt,
        )
    if errored_gt:
        query = query.filter(
            PrivacyRequest.status == PrivacyRequestStatus.error.value,
            PrivacyRequest.finished_processing_at > errored_gt,
        )

    # Conditionally embed execution log details in the response.
    if verbose:
        logger.info(f"Finding execution log details")
        PrivacyRequest.execution_logs_by_dataset = property(
            execution_logs_by_dataset_name
        )
    else:
        PrivacyRequest.execution_logs_by_dataset = property(lambda self: None)

    return paginate(query, params)


def execution_logs_by_dataset_name(
    self: PrivacyRequest,
) -> DefaultDict[str, List["ExecutionLog"]]:
    """
    Returns a truncated list of ExecutionLogs for each dataset name associated with
    a PrivacyRequest. Added as a conditional property to the PrivacyRequest class at runtime to
    show optionally embedded execution logs.

    An example response might include your execution logs from your mongo db in one group, and execution logs from
    your postgres db in a different group.
    """

    execution_logs: DefaultDict[str, List["ExecutionLog"]] = defaultdict(list)
    SessionLocal = get_db_session()
    db = SessionLocal()
    # TODO future refactor for performance
    logs = (
        ExecutionLog.query(
            db=db,
        )
        .filter(
            ExecutionLog.privacy_request_id == self.id,
        )
        .order_by(
            ExecutionLog.dataset_name,
            ExecutionLog.updated_at.asc(),
        )
    )
    for log in logs:
        if len(execution_logs[log.dataset_name]) > EMBEDDED_EXECUTION_LOG_LIMIT:
            continue
        execution_logs[log.dataset_name].append(log)
    return execution_logs


@router.get(
    urls.REQUEST_STATUS_LOGS,
    dependencies=[Security(verify_oauth_client, scopes=[scopes.PRIVACY_REQUEST_READ])],
    response_model=Page[ExecutionLogDetailResponse],
)
def get_request_status_logs(
    privacy_request_id: str,
    *,
    db: Session = Depends(deps.get_db),
    params: Params = Depends(),
) -> AbstractPage[ExecutionLog]:
    """Returns all the execution logs associated with a given privacy request ordered by updated asc."""

    logger.info(f"Finding privacy request with id '{privacy_request_id}'")

    privacy_request = PrivacyRequest.get(db, id=privacy_request_id)

    if not privacy_request:
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND,
            detail=f"No privacy request found with id '{privacy_request_id}'.",
        )

    logger.info(
        f"Finding all execution logs for privacy request {privacy_request_id} with params '{params}'"
    )

    return paginate(
        ExecutionLog.query(db=db)
        .filter(ExecutionLog.privacy_request_id == privacy_request_id)
        .order_by(ExecutionLog.updated_at.asc()),
        params,
    )


@router.put(
    REQUEST_PREVIEW,
    status_code=200,
    response_model=List[DryRunDatasetResponse],
    dependencies=[Security(verify_oauth_client, scopes=[PRIVACY_REQUEST_READ])],
)
def get_request_preview_queries(
    *,
    db: Session = Depends(deps.get_db),
    dataset_keys: Optional[List[str]] = Body(None),
) -> List[DryRunDatasetResponse]:
    """Returns dry run queries given a list of dataset ids"""
    dataset_configs: List[DatasetConfig] = []
    if not dataset_keys:
        dataset_configs = DatasetConfig.all(db=db)
        if not dataset_configs:
            raise HTTPException(
                status_code=HTTP_404_NOT_FOUND,
                detail=f"No datasets could be found",
            )
    else:
        for dataset_key in dataset_keys:
            dataset_config = DatasetConfig.get_by(
                db=db, field="fides_key", value=dataset_key
            )
            if not dataset_config:
                raise HTTPException(
                    status_code=HTTP_404_NOT_FOUND,
                    detail=f"No dataset with id '{dataset_key}'",
                )
            dataset_configs.append(dataset_config)
    try:
        connection_configs: List[ConnectionConfig] = [
            ConnectionConfig.get(db=db, id=dataset.connection_config_id)
            for dataset in dataset_configs
        ]

        dataset_graph: DatasetGraph = DatasetGraph(
            *[dataset.get_graph() for dataset in dataset_configs]
        )

        identity_seed: Dict[str, str] = {
            k: "something" for k in dataset_graph.identity_keys.values()
        }
        traversal: Traversal = Traversal(dataset_graph, identity_seed)
        queries: Dict[CollectionAddress, str] = collect_queries(
            traversal,
            TaskResources(EMPTY_REQUEST, Policy(), connection_configs),
        )
        return [
            DryRunDatasetResponse(
                collectionAddress=CollectionAddressResponse(
                    dataset=key.dataset, collection=key.collection
                ),
                query=value,
            )
            for key, value in queries.items()
        ]
    except TraversalError as err:
        logger.info(f"Dry run failed: {err}")
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=f"Dry run failed",
        )