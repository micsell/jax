#!/usr/bin/env python
# coding=utf-8
# Copyright 2023 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Fine-tuning the Flax library models for sequence to sequence speech recognition.
"""
# You can also adapt this script on your own sequence to sequence task. Pointers for this are left as comments.
import itertools
import os
import sys
import time
from dataclasses import field
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional, Union
import datasets
import numpy as np
import torch
from datasets import Dataset, DatasetDict, IterableDatasetDict, interleave_datasets, load_dataset
from torch.utils.data import IterableDataset

from huggingface_hub import Repository, create_repo
from pydub import AudioSegment
print("36")
from transformers.models.whisper.english_normalizer import BasicTextNormalizer
from transformers.file_utils import get_full_repo_name
from transformers.utils import check_min_version, send_example_telemetry
from transformers.utils.versions import require_version
import pandas as pd
import transformers
import shutil
import flax
import jax
import jax.numpy as jnp
from flax import jax_utils, traverse_util
from flax.jax_utils import pad_shard_unpad, unreplicate
from flax.training import train_state
from flax.training.common_utils import get_metrics, onehot, shard, shard_prng_key
from tqdm.notebook import tqdm
import optax
from transformers import (
    FlaxAutoModelForSpeechSeq2Seq,
    is_tensorboard_available,
    AutoConfig,
    AutoFeatureExtractor,
    AutoProcessor,
    AutoTokenizer,
    HfArgumentParser,
    Seq2SeqTrainingArguments,
)
print("63")

# Will error if the minimal version of Transformers is not installed. Remove at your own risks.
# check_min_version("4.27.0.dev0")

# require_version("datasets>=1.18.2",
#                 "To fix: pip install -r examples/flax/speech-recogintion/requirements.txt")

# logger = logging.getLogger(__name__)


@flax.struct.dataclass
class ModelArguments:
    """
    Arguments pertaining to which model/config/tokenizer we are going to fine-tune from.
    """

    model_name_or_path: str = field(
        metadata={
            "help": "Path to pretrained model or model identifier from huggingface.co/models"}
    )
    config_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained config name or path if not the same as model_name"}
    )
    tokenizer_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained tokenizer name or path if not the same as model_name"}
    )
    feature_extractor_name: Optional[str] = field(
        default=None, metadata={"help": "feature extractor name or path if not the same as model_name"}
    )
    cache_dir: Optional[str] = field(
        default=None,
        metadata={
            "help": "Where to store the pretrained models downloaded from huggingface.co"},
    )
    use_fast_tokenizer: bool = field(
        default=True,
        metadata={
            "help": "Whether to use one of the fast tokenizer (backed by the tokenizers library) or not."},
    )
    model_revision: str = field(
        default="main",
        metadata={
            "help": "The specific model version to use (can be a branch name, tag name or commit id)."},
    )
    use_auth_token: bool = field(
        default=False,
        metadata={
            "help": "Will use the token generated when running `transformers-cli login` (necessary to use this script "
            "with private models)."
        },
    )
    dtype: Optional[str] = field(
        default="float32",
        metadata={
            "help": (
                "Floating-point format in which the model weights should be initialized and trained. Choose one of"
                " `[float32, float16, bfloat16]`."
            )
        },
    )
    num_beams: Optional[int] = field(
        default=None,
        metadata={
            "help": (
                "Number of beams to use for evaluation. This argument will be passed to `model.generate`, "
                "which is used during evaluation."
            )
        },
    )


@flax.struct.dataclass
class DataTrainingArguments:
    """
    Arguments pertaining to what data we are going to input our model for training and eval.
    """

    dataset_name: Optional[str] = field(
        default=None, metadata={"help": "The name of the dataset to use (via the datasets library)."}
    )
    dataset_config_name: Optional[str] = field(
        default=None, metadata={"help": "The configuration name of the dataset to use (via the datasets library)."}
    )
    text_column: Optional[str] = field(
        default=None,
        metadata={
            "help": "The name of the column in the datasets containing the full texts (for summarization)."},
    )
    dataset_cache_dir: Optional[str] = field(
        default=None, metadata={"help": "Path to cache directory for saving and loading datasets"}
    )
    overwrite_cache: bool = field(
        default=False, metadata={"help": "Overwrite the cached training and evaluation sets"}
    )
    preprocessing_num_workers: Optional[int] = field(
        default=None,
        metadata={"help": "The number of processes to use for the preprocessing."},
    )
    max_train_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": "For debugging purposes or quicker training, truncate the number of training examples to this "
            "value if set."
        },
    )
    max_eval_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": "For debugging purposes or quicker training, truncate the number of evaluation examples to this "
            "value if set."
        },
    )
    audio_column_name: str = field(
        default="audio",
        metadata={
            "help": "The name of the dataset column containing the audio data. Defaults to 'audio'"},
    )
    text_column_name: str = field(
        default="text",
        metadata={
            "help": "The name of the dataset column containing the text data. Defaults to 'text'"},
    )
    max_duration_in_seconds: float = field(
        default=30.0,
        metadata={
            "help": "Filter audio files that are longer than `max_duration_in_seconds` seconds"},
    )
    min_duration_in_seconds: float = field(
        default=0.0,
        metadata={
            "help": "Filter audio files that are shorter than `min_duration_in_seconds` seconds"},
    )
    max_label_length: float = field(
        default=128,
        metadata={
            "help": "Truncate transcriptions that are longer `max_eval_length` tokens."},
    )
    pad_input_to_multiple_of: Optional[int] = field(
        default=None,
        metadata={
            "help": "If set will pad the input sequence to a multiple of the provided value. "
            "This is important to avoid triggering recompilations on TPU. If unspecified, will default to padding the inputs to max length."
        },
    )
    pad_target_to_multiple_of: Optional[int] = field(
        default=None,
        metadata={
            "help": "If set will pad the target sequence to a multiple of the provided value. "
            "This is important to avoid triggering recompilations on TPU. If unspecified, will default to padding the targets to max length."
        },
    )
    preprocessing_only: bool = field(
        default=False,
        metadata={
            "help": "Whether to only do data preprocessing and skip training. "
            "This is especially useful when data preprocessing errors out in distributed training due to timeout. "
            "In this case, one should run the preprocessing in a non-distributed setup with `preprocessing_only=True` "
            "so that the cached datasets can consequently be loaded in distributed training"
        },
    )
    train_split_name: str = field(
        default="train",
        metadata={
            "help": "The name of the training data set split to use (via the datasets library). Defaults to 'train'"
        },
    )
    eval_split_name: str = field(
        default="validation",
        metadata={
            "help": "The name of the evaluation data set split to use (via the datasets library). Defaults to 'validation'"
        },
    )
    do_lower_case: bool = field(
        default=True,
        metadata={"help": "Whether the target text should be lower cased."},
    )
    do_remove_punctuation: bool = field(
        default=False,
        metadata={
            "help": "Whether the target text should be striped of punctuation."},
    )
    do_normalize_eval: bool = field(
        default=True,
        metadata={
            "help": "Whether to normalise the references and predictions in the eval WER calculation."},
    )
    language: str = field(
        default=None,
        metadata={
            "help": (
                "Language for multilingual fine-tuning. This argument should be set for multilingual fine-tuning "
                "only. For English speech recognition, it should be set to `None`."
            )
        },
    )
    task: str = field(
        default="transcribe",
        metadata={
            "help": "Task, either `transcribe` for speech recognition or `translate` for speech translation."},
    )
    num_train_steps: int = field(default=50000, metadata={
                                 "help": "The number of training steps."})
    shuffle_buffer_size: Optional[int] = field(
        default=500,
        metadata={
            "help": (
                "The number of streamed examples to download before shuffling them. The large the buffer, "
                "the closer it is to real offline shuffling."
            )
        },
    )
    streaming: bool = field(
        default=True,
        metadata={
            "help": "Whether to use streaming mode to load and pre-process the data."},
    )
    number_write_predictions: Optional[int] = field(
        default=0,
        metadata={
            "help": "If set to a non-zero value, this indicates the number of predicitons from the evaluation set that is written to the predictions folder. Requires --predict_with_generate to be set."},
    )


def shift_tokens_right(label_ids: np.array, decoder_start_token_id: int) -> np.ndarray:
    """
    Shift label ids one token to the right.
    """
    shifted_label_ids = np.zeros_like(label_ids)
    shifted_label_ids[:, 1:] = label_ids[:, :-1]
    shifted_label_ids[:, 0] = decoder_start_token_id

    return shifted_label_ids


@flax.struct.dataclass
class FlaxDataCollatorSpeechSeq2SeqWithPadding:
    """
    Data collator that will dynamically pad the inputs received.
    Args:
        processor ([`Wav2Vec2Processor`])
            The processor used for proccessing the data.
        decoder_start_token_id (:obj: `int`)
            The begin-of-sentence of the decoder.
        input_padding (:obj:`bool`, :obj:`str` or :class:`~transformers.tokenization_utils_base.PaddingStrategy`, `optional`, defaults to :obj:`True`):
            Select a strategy to pad the returned input sequences (according to the model's padding side and padding index)
            among:
            * :obj:`True` or :obj:`'longest'`: Pad to the longest sequence in the batch (or no padding if only a single
              sequence if provided).
            * :obj:`'max_length'`: Pad to a maximum length specified with the argument :obj:`max_length` or to the
              maximum acceptable input length for the model if that argument is not provided.
            * :obj:`False` or :obj:`'do_not_pad'` (default): No padding (i.e., can output a batch with sequences of
              different lengths).
        target_padding (:obj:`bool`, :obj:`str` or :class:`~transformers.tokenization_utils_base.PaddingStrategy`, `optional`, defaults to :obj:`True`):
            Select a strategy to pad the returned target sequences (according to the model's padding side and padding index).
            See above for details.
        max_input_length (:obj:`float`, `optional`):
            Maximum length of the ``input_values`` of the returned list and optionally padding length (see above).
        max_target_length (:obj:`int`, `optional`):
            Maximum length of the ``labels`` of the returned list and optionally padding length (see above).
        pad_input_to_multiple_of (:obj:`int`, `optional`):
            If set will pad the input sequence to a multiple of the provided value.
            This is especially useful to enable the use of Tensor Cores on NVIDIA hardware with compute capability >=
            7.5 (Volta).
        pad_target_to_multiple_of (:obj:`int`, `optional`):
            If set will pad the target sequence to a multiple of the provided value.
            This is especially useful to enable the use of Tensor Cores on NVIDIA hardware with compute capability >=
            7.5 (Volta).
    """

    processor: Any
    decoder_start_token_id: int
    input_padding: Union[bool, str] = "longest"
    target_padding: Union[bool, str] = "max_length"
    max_input_length: Optional[float] = None
    max_target_length: Optional[int] = None
    pad_input_to_multiple_of: Optional[int] = None
    pad_target_to_multiple_of: Optional[int] = None

    def __call__(self, features: List[Dict[str, Union[List[int], np.ndarray]]]) -> Dict[str, np.ndarray]:
        model_input_name = self.processor.model_input_names[0]
        input_features = {model_input_name: features[model_input_name]}
        label_features = {"input_ids": features["labels"]}

        # reformat list to dict and set to pytorch format
        batch = self.processor.feature_extractor.pad(
            input_features,
            max_length=self.max_input_length,
            padding=self.input_padding,
            pad_to_multiple_of=self.pad_input_to_multiple_of,
            return_tensors="np",
        )

        labels_batch = self.processor.tokenizer.pad(
            label_features,
            max_length=self.max_target_length,
            padding=self.target_padding,
            pad_to_multiple_of=self.pad_target_to_multiple_of,
            return_tensors="np",
        )

        # if bos token is appended in previous tokenization step,
        # cut bos token here as it's append later anyways
        labels = labels_batch["input_ids"]
        if (labels[:, 0] == self.decoder_start_token_id).all().item():
            labels = labels[:, 1:]
            labels_batch.attention_mask = labels_batch.attention_mask[:, 1:]

        decoder_input_ids = shift_tokens_right(
            labels, self.decoder_start_token_id)

        # replace padding with -100 to ignore correctly when computing the loss
        labels = np.ma.array(labels, mask=np.not_equal(
            labels_batch.attention_mask, 1))
        labels = labels.filled(fill_value=-100)

        batch["labels"] = labels
        batch["decoder_input_ids"] = decoder_input_ids

        return batch


# def load_maybe_streaming_dataset(dataset_name, dataset_config_name, split="train", streaming=True, **kwargs):
#     """
#     Utility function to load a dataset in streaming mode. For datasets with multiple splits,
#     each split is loaded individually and then splits combined by taking alternating examples from
#     each (interleaving).
#     """
#     if "+" in split:
#         # load multiple splits separated by the `+` symbol with streaming mode
#         dataset_splits = [
#             load_dataset(dataset_name, dataset_config_name,
#                          split=split_name, streaming=streaming, **kwargs)
#             for split_name in split.split("+")
#         ]
#         # interleave multiple splits to form one dataset
#         interleaved_dataset = interleave_datasets(dataset_splits)
#         return interleaved_dataset
#     else:
#         # load a single split *with* streaming mode
#         dataset = load_dataset(
#             dataset_name, dataset_config_name, split=split, streaming=streaming, **kwargs)
#         return dataset


def collate_batch(samples):
    return {key: [feature[key] for feature in samples] for key in samples[0]}


def data_loader(
    dataset: Dataset,
    batch_size: int,
    drop_last: bool = True,
    num_workers: int = 0,
) -> Generator:
    """
    Returns batches of size `batch_size` from `dataset`. If `drop_last` is set to `False`, the final batch may be incomplete,
    and range in size from 1 to `batch_size`. Shuffle batches if `shuffle` is `True`.
    """
    data_loader_iterator = iter(torch.utils.data.DataLoader(
        batch_size=batch_size,
        dataset=dataset.with_format("torch"),
        num_workers=num_workers,
        collate_fn=collate_batch,
        drop_last=drop_last,
    ))
    return data_loader_iterator


class TrainState(train_state.TrainState):
    dropout_rng: jnp.ndarray

    def replicate(self):
        return jax_utils.replicate(self).replace(dropout_rng=shard_prng_key(self.dropout_rng))


def write_metric(summary_writer, train_metrics, eval_metrics, train_time, step):
    summary_writer.scalar("train_time", train_time, step)

    train_metrics = get_metrics(train_metrics)
    for key, vals in train_metrics.items():
        tag = f"train_{key}"
        for i, val in enumerate(vals):
            summary_writer.scalar(tag, val, step - len(vals) + i + 1)

    for metric_name, value in eval_metrics.items():
        summary_writer.scalar(f"eval_{metric_name}", value, step)


def create_learning_rate_fn(
    num_train_steps: int, num_warmup_steps: int, learning_rate: float, warmup_init_value: float = 0.0, decay_end_value: float = 0.0,
) -> Callable[[int], jnp.array]:
    """Returns a linear warmup, linear_decay learning rate function."""
    warmup_fn = optax.linear_schedule(
        init_value=warmup_init_value, end_value=learning_rate, transition_steps=num_warmup_steps)
    decay_fn = optax.linear_schedule(
        init_value=learning_rate, end_value=decay_end_value, transition_steps=num_train_steps - num_warmup_steps
    )
    schedule_fn = optax.join_schedules(
        schedules=[warmup_fn, decay_fn], boundaries=[num_warmup_steps])
    return schedule_fn


def main(args):
    print("467")

    # 1. Parse input arguments
    # See all possible arguments in src/transformers/training_args.py
    # or by passing the --help flag to this script.
    # We now keep distinct sets of args, for a cleaner separation of concerns.
    parser = HfArgumentParser(
        (ModelArguments, DataTrainingArguments, Seq2SeqTrainingArguments))
    print("475")
    # if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
    #     # If we pass only one argument to the script and it's the path to a json file,
    #     # let's parse it to get our arguments.
    #     model_args, data_args, training_args = parser.parse_json_file(
    #         json_file=os.path.abspath(sys.argv[1]))
    # else:
    model_args, data_args, training_args = parser.parse_dict(args, True)
    print("483")
    print(model_args, data_args, training_args)
    print("485")
    # Sending telemetry. Tracking the example usage helps us better allocate resources to maintain them. The
    # information sent is the one passed as arguments along with your JAX/Flax versions.
    send_example_telemetry("run_speech_recognition_seq2seq",
                           model_args, data_args, framework="flax")

    # 2. Setup logging
    # Make one log on every process with the configuration for debugging.
    # logging.basicConfig(
    #     format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    #     datefmt="%m/%d/%Y %H:%M:%S",
    #     handlers=[logging.StreamHandler(sys.stderr)],
    # )
    # # Set the verbosity to info of the Transformers logger.
    # # We only want one process per machine to log things on the screen.
    # logger.setLevel(logging.INFO if jax.process_index()
    #                 == 0 else logging.ERROR)
    if jax.process_index() == 0:
        datasets.utils.logging.set_verbosity_warning()
        transformers.utils.logging.set_verbosity_info()
    else:
        datasets.utils.logging.set_verbosity_error()
        transformers.utils.logging.set_verbosity_error()

    print("Training/evaluation parameters %s", training_args)

    # Check the output dir is valid
    if (
        os.path.exists(training_args.output_dir)
        and os.listdir(training_args.output_dir)
        and training_args.do_train
        and not training_args.overwrite_output_dir
    ):
        raise ValueError(
            f"Output directory ({training_args.output_dir}) already exists and is not empty."
            "Use `--overwrite_output_dir` to overcome."
        )

    # Handle the repository creation
    if training_args.push_to_hub:
        if training_args.hub_model_id is None:
            repo_name = get_full_repo_name(
                Path(training_args.output_dir).absolute().name,
                token=training_args.hub_token,
                organization=training_args.push_to_hub_organization,
            )
        else:
            repo_name = training_args.hub_model_id
        create_repo(
            repo_name, exist_ok=True, token=training_args.hub_token, private=training_args.hub_private_repo
        )
        repo = Repository(training_args.output_dir,
                          clone_from=repo_name, token=training_args.hub_token)

    # 3. Load dataset
    os.environ["HF_TOKEN"] = "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"

    raw_datasets = IterableDatasetDict()
    hebrew_train = load_dataset("micsell/hebrew_speech_kan_nikud", 'default', split="train", streaming=True)
    hebrew_test = load_dataset("micsell/hebrew_speech_kan_nikud", 'default', split="test", streaming=True)
    english_train = load_dataset("mozilla-foundation/common_voice_16_1", 'en', split="train", streaming=True, token="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")
    english_train = english_train.cast_column("audio", Audio(sampling_rate=16000))
    english_test = load_dataset("mozilla-foundation/common_voice_16_1", 'en', split="test", streaming=True, token = "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")
    english_test = english_test.cast_column("audio", Audio(sampling_rate=16000))
    train = [hebrew_train, english_train]
    test = [hebrew_test, english_test]
    interleaved_train = interleave_datasets(train)
    interleaved_test = interleave_datasets(test)


    raw_datasets["train"] = interleaved_train
    raw_datasets["test"] = interleaved_test


    raw_datasets_features = list(
        next(iter(raw_datasets.values())).features.keys())


    # 5. Load pretrained model, tokenizer, and feature extractor
    config = AutoConfig.from_pretrained(
        model_args.config_name if model_args.config_name else model_args.model_name_or_path,
        cache_dir=model_args.cache_dir,
        revision=model_args.model_revision,
        use_auth_token=True if model_args.use_auth_token else None,
    )
    feature_extractor = AutoFeatureExtractor.from_pretrained(
        model_args.feature_extractor_name if model_args.feature_extractor_name else model_args.model_name_or_path,
        cache_dir=model_args.cache_dir,
        revision=model_args.model_revision,
        use_auth_token=True if model_args.use_auth_token else None,
    )
    tokenizer = AutoTokenizer.from_pretrained(
        model_args.tokenizer_name if model_args.tokenizer_name else model_args.model_name_or_path,
        cache_dir=model_args.cache_dir,
        use_fast=model_args.use_fast_tokenizer,
        revision=model_args.model_revision,
        use_auth_token=True if model_args.use_auth_token else None,
    )

    model = FlaxAutoModelForSpeechSeq2Seq.from_pretrained(
        model_args.model_name_or_path,
        config=config,
        dtype=getattr(jnp, model_args.dtype),
        cache_dir=model_args.cache_dir,
        revision=model_args.model_revision,
        use_auth_token=True if model_args.use_auth_token else None,
    )

    if model.config.decoder_start_token_id is None:
        raise ValueError(
            "Make sure that `config.decoder_start_token_id` is correctly defined")

    # 6. Resample speech dataset: `datasets` takes care of automatically loading and resampling the audio,
    # so we just need to set the correct target sampling rate.
    dataset_sampling_rate = next(
        iter(raw_datasets.values())).features[data_args.audio_column_name].sampling_rate

    if dataset_sampling_rate != feature_extractor.sampling_rate:
        raw_datasets = raw_datasets.cast_column(
            data_args.audio_column_name, datasets.features.Audio(
                sampling_rate=feature_extractor.sampling_rate)
        )

    # 7. Preprocessing the datasets.
    # We need to read the audio files as arrays and tokenize the targets.
    max_input_length = int(
        data_args.max_duration_in_seconds * feature_extractor.sampling_rate)
    min_input_length = int(
        data_args.min_duration_in_seconds * feature_extractor.sampling_rate)
    max_label_length = (
        data_args.max_label_length if data_args.max_label_length is not None else model.config.max_length
    )
    pad_input_to_multiple_of = data_args.pad_input_to_multiple_of
    pad_target_to_multiple_of = data_args.pad_target_to_multiple_of
    audio_column_name = data_args.audio_column_name
    num_workers = data_args.preprocessing_num_workers
    text_column_name = data_args.text_column_name
    model_input_name = feature_extractor.model_input_names[0]
    do_lower_case = data_args.do_lower_case
    do_remove_punctuation = data_args.do_remove_punctuation
    normalizer = BasicTextNormalizer()  # 'official' text normalizer from OpenAI

    if data_args.language is not None:
        # We only need to set the task id when the language is specified (i.e. in a multilingual setting)
        tokenizer.set_prefix_tokens(
            language=data_args.language, task=data_args.task)

    def prepare_dataset(batch):
        # process audio
        sample = batch[audio_column_name]
        inputs = feature_extractor(
            sample["array"], sampling_rate=sample["sampling_rate"])
        # process audio length
        batch[model_input_name] = inputs.get(model_input_name)[0]
        batch["input_length"] = len(sample["array"])

        # process targets
        input_str = batch[text_column_name].lower(
        ) if do_lower_case else batch[text_column_name]
        if do_remove_punctuation:
            input_str = normalizer(input_str).strip()
        if batch.get("language"):
            language = batch.get("language")
        else:
            language = "English"
        tokenizer.set_prefix_tokens(language=language, task="transcribe")
        batch["labels"] = tokenizer(input_str).input_ids
        return batch

    with training_args.main_process_first(desc="dataset map pre-processing"):
        vectorized_datasets = raw_datasets.map(
            prepare_dataset,
            remove_columns=raw_datasets_features,
        )

    # filter training data with inputs longer than max_input_length
    def is_audio_in_length_range(length):
        return min_input_length < length < max_input_length

    if training_args.do_train:
        vectorized_datasets["train"] = vectorized_datasets["train"].filter(
            is_audio_in_length_range,
            input_columns=["input_length"],
        )

    if training_args.do_eval:
        vectorized_datasets["eval"] = vectorized_datasets["eval"].filter(
            is_audio_in_length_range,
            input_columns=["input_length"],
        )

    # 8. Load Metric and write stats
    # metric_wer = evaluate.load("wer")
    # metric_cer = evaluate.load("cer")
    do_normalize_eval = data_args.do_normalize_eval

    def compute_metrics(pred_ids, label_ids):
        # replace padded labels by the padding token
        for idx in range(len(label_ids)):
            label_ids[idx][label_ids[idx] == -100] = tokenizer.pad_token_id

        pred_str = tokenizer.batch_decode(pred_ids, skip_special_tokens=True)
        # we do not want to group tokens when computing the metrics
        label_str = tokenizer.batch_decode(label_ids, skip_special_tokens=True)

        if do_normalize_eval:
            pred_str = [normalizer(pred) for pred in pred_str]
            label_str = [normalizer(label) for label in label_str]
            # filtering step to only evaluate the samples that correspond to non-zero references:
            pred_str = [pred_str[i]
                        for i in range(len(pred_str)) if len(label_str[i]) > 0]
            label_str = [label_str[i]
                         for i in range(len(label_str)) if len(label_str[i]) > 0]

        # wer = 100 * \
        #     metric_wer.compute(predictions=pred_str, references=label_str)
        # cer = 100 * \
        #     metric_cer.compute(predictions=pred_str, references=label_str)
        wer = 1
        cer = 1
        return {"wer": wer, "cer": cer}

    # TODO: Sanchit. This part is not essential and a bit verbose.
    # We do howver think it is extemely useful info for debugging and improving the model

    def write_predictions(step, eval_samples, eval_metrics, pred_ids, label_ids):
        predictions_folder_name = os.path.join(
            training_args.output_dir, "predictions")
        eval_table = f"| STEP| loss | wer |cer|\n| ---| --- | --- |--- |\n| **{step}**| {eval_metrics['loss']:.3f} | {eval_metrics['wer']:.3f} |{eval_metrics['cer']:.3f} |"

        # Put all predictions into a table
        inference_df = pd.DataFrame(columns=['mp3', 'target', 'prediction'])

        idx = 0
        for pred, label in zip(pred_ids, label_ids):
            pred_text = tokenizer.decode(pred, skip_special_tokens=True)
            label_text = tokenizer.decode(label, skip_special_tokens=True)
            formatted_pred_text = " ".join(
                [f"**{word}**" if word in label_text else f"*{word}*" for word in pred_text.split()])
            #wer = 100 * metric_wer.compute(predictions=[pred_text], references=[label_text])
            #wer_formatted = f'{wer:.2f}'
            audio_control = f'[↓]({"mp3/pred_"+str(idx)+".mp3"})'
            new_row = pd.DataFrame(
                {'mp3': audio_control, 'target': label_text, 'prediction': formatted_pred_text}, index=[0])
            inference_df = pd.concat(
                [inference_df, new_row], ignore_index=True)
            idx += 1

        # Create the prediction table of the first N rows
        inference_df = inference_df[['mp3', 'target', 'prediction']]
        predict_table = inference_df[0:data_args.number_write_predictions].to_markdown(
            index=False)

        # Build the markdown page
        markdown_str = f"{eval_table}\n\n{predict_table}"

        # Save the stats file
        stats_file_name = f"{predictions_folder_name}/step_{step}.md"
        with open(stats_file_name, "w") as f:
            f.write(markdown_str)

        # Create an header for all the files
        md_files = sorted(os.path.basename(file) for file in os.listdir(
            predictions_folder_name) if file.startswith("step_"))
        sorted_md_files = sorted(
            md_files, key=lambda x: int(x[0:-3].split("_")[1]))
        md_header = " | ".join(
            f"[Step {file[:-3].split('_')[1]}]({file})" for file in sorted_md_files)

        # Add this header to all the stats file in the folder
        for filename in os.listdir(predictions_folder_name):
            if filename.startswith("step_"):
                with open(os.path.join(predictions_folder_name, filename), "r+") as f:
                    content = f.read()
                    new_content = md_header + "\n\n" + \
                        content[content.index("| STEP| loss | wer"):]
                    f.seek(0)
                    f.write(new_content)
                    f.truncate()

        # Add a folder for the mp3 files
        if not os.path.exists(os.path.join(predictions_folder_name, "mp3")):
            os.makedirs(os.path.join(predictions_folder_name, "mp3"))

        for idx, mp3array in enumerate(eval_samples[0:data_args.number_write_predictions]):
            audio_segment = AudioSegment(data=mp3array.tobytes(
            ), sample_width=mp3array.dtype.itemsize, frame_rate=16000, channels=1)
            file_path = os.path.join(
                predictions_folder_name, "mp3") + "/pred_" + str(idx) + ".mp3"
            if not os.path.exists(file_path):
                audio_segment.export(file_path, format="mp3")

        print(
            f"Created {stats_file_name} and updated the headers of the other stats files")

    # 9. Save feature extractor, tokenizer and config
    feature_extractor.save_pretrained(training_args.output_dir)
    tokenizer.save_pretrained(training_args.output_dir)
    config.save_pretrained(training_args.output_dir)

    processor = AutoProcessor.from_pretrained(training_args.output_dir)

    data_collator = FlaxDataCollatorSpeechSeq2SeqWithPadding(
        processor=processor,
        decoder_start_token_id=model.config.decoder_start_token_id,
        input_padding="longest",
        target_padding="longest",
        max_target_length=max_label_length,
        pad_input_to_multiple_of=pad_input_to_multiple_of,
        pad_target_to_multiple_of=pad_target_to_multiple_of if pad_target_to_multiple_of else max_label_length,
    )

    # Enable tensorboard only on the master node
    has_tensorboard = is_tensorboard_available()
    if has_tensorboard and jax.process_index() == 0:
        try:
            from flax.metrics.tensorboard import SummaryWriter

            summary_writer = SummaryWriter(
                log_dir=Path(os.path.join(training_args.output_dir, "events")))
        except ImportError as ie:
            has_tensorboard = False
            print(
                f"Unable to display metrics through TensorBoard because some package are not installed: {ie}"
            )
    else:
        print(
            "Unable to display metrics through TensorBoard because the package is not installed: "
            "Please run pip install tensorboard to enable."
        )

    # Initialize our training
    rng = jax.random.PRNGKey(training_args.seed)
    rng, dropout_rng = jax.random.split(rng)
    # rng, input_rng = jax.random.split(rng)

    # Store some constant
    #num_epochs = int(training_args.num_train_epochs)
    train_batch_size = int(
        training_args.per_device_train_batch_size) * jax.device_count()
    eval_batch_size = int(
        training_args.per_device_eval_batch_size) * jax.device_count()

    # Create learning rate schedule
    lr_scheduler_types = {"linear", "constant", "constant_with_warmup"}
    if training_args.lr_scheduler_type not in lr_scheduler_types:
        raise ValueError(
            f"lr_scheduler_type of type {training_args.lr_scheduler_type} not supported, choose from {lr_scheduler_types}."
        )
    elif training_args.lr_scheduler_type == "constant":
        warmup_init_value = training_args.learning_rate
        decay_end_value = training_args.learning_rate
    elif training_args.lr_scheduler_type == "constant_with_warmup":
        warmup_init_value = 0.0
        decay_end_value = training_args.learning_rate
    else:
        warmup_init_value = 0.0
        decay_end_value = 0.0
    linear_decay_lr_schedule_fn = create_learning_rate_fn(
        data_args.num_train_steps * train_batch_size,
        training_args.warmup_steps,
        training_args.learning_rate,
        warmup_init_value=warmup_init_value,
        decay_end_value=decay_end_value,
    )

    # We use Optax's "masking" functionality to not apply weight decay
    # to bias and LayerNorm scale parameters. decay_mask_fn returns a
    # mask boolean with the same structure as the parameters.
    # The mask is True for parameters that should be decayed.
    def decay_mask_fn(params):
        flat_params = traverse_util.flatten_dict(params)
        # find out all LayerNorm parameters
        layer_norm_candidates = ["layernorm", "layer_norm", "ln"]
        layer_norm_named_params = set(
            [
                layer[-2:]
                for layer_norm_name in layer_norm_candidates
                for layer in flat_params.keys()
                if layer_norm_name in "".join(layer).lower()
            ]
        )
        flat_mask = {path: (path[-1] != "bias" and path[-2:]
                            not in layer_norm_named_params) for path in flat_params}
        return traverse_util.unflatten_dict(flat_mask)

    # create adam optimizer
    adamw = optax.adamw(
        learning_rate=linear_decay_lr_schedule_fn,
        b1=training_args.adam_beta1,
        b2=training_args.adam_beta2,
        eps=training_args.adam_epsilon,
        weight_decay=training_args.weight_decay,
        mask=decay_mask_fn,
    )

    # Setup train state
    state = TrainState.create(
        apply_fn=model.__call__, params=model.params, tx=adamw, dropout_rng=dropout_rng)

    # label smoothed cross entropy
    def loss_fn(logits, labels, label_smoothing_factor=0.0):
        """
        The label smoothing implementation is adapted from Flax's official example:
        https://github.com/google/flax/blob/87a211135c6a377c8f29048a1cac3840e38b9da4/examples/wmt/train.py#L104
        """
        vocab_size = logits.shape[-1]
        confidence = 1.0 - label_smoothing_factor
        low_confidence = (1.0 - confidence) / (vocab_size - 1)
        normalizing_constant = -(
            confidence * jnp.log(confidence) + (vocab_size - 1) *
            low_confidence * jnp.log(low_confidence + 1e-20)
        )
        soft_labels = onehot(labels, vocab_size,
                             on_value=confidence, off_value=low_confidence)

        loss = optax.softmax_cross_entropy(logits, soft_labels)
        loss = loss - normalizing_constant

        # ignore padded tokens from loss, i.e. where labels are not set to -100
        padding_mask = labels >= 0
        loss = loss * padding_mask
        loss = loss.sum()
        num_labels = padding_mask.sum()
        return loss, num_labels

    # Define gradient update step fn
    def train_step(state, batch, label_smoothing_factor=0.0):
        dropout_rng, new_dropout_rng = jax.random.split(state.dropout_rng)

        def compute_loss(params):
            labels = batch.pop("labels")
            logits = state.apply_fn(
                **batch, params=params, dropout_rng=dropout_rng, train=True)[0]
            loss, num_labels = loss_fn(logits, labels, label_smoothing_factor)
            return loss, num_labels

        grad_fn = jax.value_and_grad(compute_loss, has_aux=True)
        (loss, num_labels), grad = grad_fn(state.params)
        num_labels = jax.lax.psum(num_labels, "batch")

        # true loss = total loss / total samples
        loss = jax.lax.psum(loss, "batch")
        loss = jax.tree_util.tree_map(lambda x: x / num_labels, loss)

        # true grad = total grad / total samples
        grad = jax.lax.psum(grad, "batch")
        grad = jax.tree_util.tree_map(lambda x: x / num_labels, grad)
        new_state = state.apply_gradients(
            grads=grad, dropout_rng=new_dropout_rng)

        metrics = {"loss": loss,
                   "learning_rate": linear_decay_lr_schedule_fn(state.step)}
        return new_state, metrics

    # Define eval fn
    def eval_step(params, batch, label_smoothing_factor=0.0):
        labels = batch.pop("labels")
        logits = model(**batch, params=params, train=False)[0]

        loss, num_labels = loss_fn(logits, labels, label_smoothing_factor)
        num_labels = jax.lax.psum(num_labels, "batch")

        # true loss = total loss / total samples
        loss = jax.lax.psum(loss, "batch")
        loss = jax.tree_util.tree_map(lambda x: x / num_labels, loss)

        metrics = {"loss": loss}
        return metrics

    # Define generation function
    num_beams = model_args.num_beams if model_args.num_beams is not None else model.config.num_beams
    gen_kwargs = {"max_length": max_label_length, "num_beams": num_beams}

    def generate_step(params, batch):
        model.params = params
        output_ids = model.generate(batch[model_input_name], attention_mask=batch.get(
            "attention_mask"), **gen_kwargs)
        return output_ids.sequences

    # Clean up the prediction folder if write_predictions is set to True
    if data_args.number_write_predictions:
        predictions_folder_name = os.path.join(
            training_args.output_dir, "predictions")
        shutil.rmtree(predictions_folder_name, ignore_errors=True)
        os.makedirs(predictions_folder_name, exist_ok=True)
        print(f"Created folder {predictions_folder_name}")

    # Create parallel version of the train and eval step
    p_train_step = jax.pmap(
        partial(train_step, label_smoothing_factor=training_args.label_smoothing_factor), "batch", donate_argnums=(0, )
    )
    p_eval_step = jax.pmap(partial(
        eval_step, label_smoothing_factor=training_args.label_smoothing_factor), "batch")
    p_generate_step = jax.pmap(generate_step, "batch")

    # Replicate the train state on each device
    state = state.replicate()

    print("***** Running training *****")
    print(
        f"  Num examples = {data_args.num_train_steps * train_batch_size}")
    print(
        f"  Instantaneous batch size per device = {training_args.per_device_train_batch_size}")
    print(
        f"  Total train batch size (w. parallel & distributed) = {train_batch_size}")
    print(f"  Total optimization steps = {data_args.num_train_steps}")

    train_time = 0

    # ======================== Training ================================
    train_start = time.time()

    train_metrics = []
    epoch = 0
    train_dataset = vectorized_datasets["train"].shuffle(
        seed=training_args.seed, buffer_size=data_args.shuffle_buffer_size)
    eval_dataset = vectorized_datasets["eval"]
    train_loader = data_loader(train_dataset, train_batch_size)
    print("A")
    # train
    for step in tqdm(range(data_args.num_train_steps), desc="Training...", position=1, leave=False):
        try:
            samples = next(train_loader)
        except StopIteration:
            epoch += 1
            train_dataset.set_epoch(epoch)
            train_loader = data_loader(train_dataset, train_batch_size)
            samples = next(train_loader)
            print(
                f"Completed epoch ({epoch} | Loss: {train_metric['loss']}, Learning Rate:"
                f" {train_metric['learning_rate']})"
            )

        batch = data_collator(samples)
        batch = shard(batch.data)
        state, train_metric = p_train_step(state, batch)
        train_metrics.append(train_metric)

        train_time += time.time() - train_start
        train_metric = unreplicate(train_metric)
        # ======================== Evaluating ==============================
        if step % training_args.eval_steps == 0 and step > 0:
            eval_metrics = []
            eval_preds = []
            eval_labels = []
            eval_samples = []
            eval_loader = data_loader(
                eval_dataset, eval_batch_size, drop_last=False)
            if data_args.max_eval_samples:
                max_eval_steps_iter = range(
                    1 + data_args.max_eval_samples // eval_batch_size)
            else:
                max_eval_steps_iter = itertools.repeat(None)
            for _ in tqdm(max_eval_steps_iter, desc="Evaluating...", position=2, leave=False):
                # Model forward
                try:
                    samples = next(eval_loader)
                except StopIteration:
                    break
                batch = data_collator(samples)
                labels = batch["labels"]

                metrics = pad_shard_unpad(p_eval_step, static_return=True)(
                    state.params, batch.data, min_device_batch=training_args.per_device_eval_batch_size
                )
                eval_metrics.append(metrics)
                if training_args.predict_with_generate and data_args.number_write_predictions and len(eval_samples) < data_args.number_write_predictions+eval_batch_size:
                    eval_samples.extend(samples['input_features'])

                # generation
                if training_args.predict_with_generate:
                    generated_ids = pad_shard_unpad(
                        p_generate_step)(state.params, batch.data)
                    eval_preds.extend(jax.device_get(
                        generated_ids.reshape(-1, gen_kwargs["max_length"])))
                    eval_labels.extend(labels)

            # normalize eval metrics
            eval_metrics = get_metrics(eval_metrics)
            eval_metrics = jax.tree_util.tree_map(jnp.mean, eval_metrics)

            # compute metrics
            metric_desc = ""
            if training_args.predict_with_generate:
                # metric_values = compute_metrics(eval_preds, eval_labels)
                {"wer": 2}
                metric_values = eval_metrics.update(metric_values)
                metric_desc = " ".join(
                    [f"Eval {key}: {value} |" for key, value in metric_values.items()])

            # Print metrics
            desc = f"Epoch... ({epoch} | Eval Loss: {eval_metrics['loss']} | {metric_desc})"
            print(desc)

            # Save metrics
            if has_tensorboard and jax.process_index() == 0:
                write_metric(summary_writer, train_metrics,
                             eval_metrics, train_time, step)

            if training_args.predict_with_generate and data_args.number_write_predictions:
                write_predictions(step, eval_samples,
                                  eval_metrics, eval_preds, eval_labels)

            # save checkpoint after each epoch and push checkpoint to the hub
            if jax.process_index() == 0:
                params = jax.device_get(
                    jax.tree_util.tree_map(lambda x: x[0], state.params))
                model.save_pretrained(training_args.output_dir, params=params)
                tokenizer.save_pretrained(training_args.output_dir)
                if training_args.push_to_hub:
                    repo.push_to_hub(
                        commit_message=f"Saving weights and logs of epoch {epoch}", blocking=False)