# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved

import functools
import boto3
import os
import datetime
import json
import typing as t

from mypy_boto3_dynamodb import DynamoDBServiceResource
from mypy_boto3_sqs import SQSClient

from threatexchange.content_type.meta import get_content_type_for_name
from threatexchange.content_type.photo import PhotoContent
from threatexchange.content_type.video import VideoContent
from threatexchange.signal_type.md5 import VideoMD5Signal
from threatexchange.signal_type.pdq import PdqSignal

from hmalib.common.logging import get_logger
from hmalib import metrics
from hmalib.common.messages.submit import (
    S3ImageSubmission,
    S3ImageSubmissionBatchMessage,
    URLSubmissionMessage,
)
from hmalib.hashing.unified_hasher import UnifiedHasher
from hmalib.common.models.pipeline import PipelineHashRecord
from hmalib.common.content_sources import S3BucketContentSource, URLContentSource

logger = get_logger(__name__)
sqs_client = boto3.client("sqs")


@functools.lru_cache(maxsize=None)
def get_dynamodb() -> DynamoDBServiceResource:
    return boto3.resource("dynamodb")


@functools.lru_cache(maxsize=None)
def get_sqs_client() -> SQSClient:
    return boto3.client("sqs")


OUTPUT_QUEUE_URL = os.environ["HASHES_QUEUE_URL"]
DYNAMODB_TABLE = os.environ["DYNAMODB_TABLE"]
IMAGE_PREFIX = os.environ["IMAGE_PREFIX"]


# If you want to support additional content or signal types, they can be added
# here.
hasher = UnifiedHasher(
    supported_content_types=[PhotoContent, VideoContent],
    supported_signal_types=[PdqSignal, VideoMD5Signal],
    output_queue_url=OUTPUT_QUEUE_URL,
)


def lambda_handler(event, context):
    """
    SQS Events generated by the submissions API or by files being added to S3.
    Downloads files to temp-storage, identifies content_type and generates
    allowed signal_types from it.

    Saves hash output to DynamoDB, sends a message on an output queue.

    Note that this brings the contents of a file into memory. This is subject to
    the resource limitation on the lambda. Potentially extendable until 10GB, but
    that would be super-expensive. [1]

    [1]: https://docs.aws.amazon.com/lambda/latest/dg/configuration-console.html
    """
    records_table = get_dynamodb().Table(DYNAMODB_TABLE)
    sqs_client = get_sqs_client()

    for sqs_record in event["Records"]:
        message = json.loads(sqs_record["body"])

        if message.get("Event") == "s3:TestEvent":
            continue

        media_to_process: t.List[t.Union[S3ImageSubmission, URLSubmissionMessage]] = []

        if URLSubmissionMessage.could_be(message):
            media_to_process.append(URLSubmissionMessage.from_sqs_message(message))
        elif S3ImageSubmissionBatchMessage.could_be(message):
            # S3 submissions can only be images for now.
            media_to_process.extend(
                S3ImageSubmissionBatchMessage.from_sqs_message(
                    message, image_prefix=IMAGE_PREFIX
                ).image_submissions
            )
        else:
            logger.warn(f"Unprocessable Message: {message}")

        for media in media_to_process:
            if not hasher.supports(media.content_type):
                logger.warn(f"Unprocessable content type: {media.content_type}")
                continue

            with metrics.timer(metrics.names.hasher.download_file):
                if hasattr(media, "key") and hasattr(media, "bucket"):
                    # Classic duck-typing. If it has key and bucket, must be an
                    # S3 submission.
                    bytes_: bytes = S3BucketContentSource(
                        media.bucket, IMAGE_PREFIX
                    ).get_bytes(media.content_id)
                else:
                    bytes_: bytes = URLContentSource().get_bytes(media.url)

            for signal in hasher.get_hashes(
                media.content_id, media.content_type, bytes_
            ):
                hash_record = PipelineHashRecord(
                    content_id=media.content_id,
                    signal_type=signal.signal_type,
                    content_hash=signal.signal_value,
                    updated_at=datetime.datetime.now(),
                )

                hasher.write_hash_record(records_table, hash_record)
                hasher.publish_hash_message(sqs_client, hash_record)

    metrics.flush()
