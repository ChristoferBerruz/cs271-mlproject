from dataclasses import dataclass, field
from typing import List, Tuple, Dict
import polars as pl
import torch
import torchvision.transforms as transforms
from torchvision.datasets import ImageFolder
from torch.utils.data import Dataset
import numpy as np
from mlproject.data_processing import RawHumanChatBotData
from mlproject.embeddings import ArticleEmbedder

import os
from tqdm import tqdm

from abc import abstractmethod, ABC


@dataclass
class HumanChatBotDataset(Dataset):
    """
    A PyTorch Dataset class for the Human Chat Bot dataset.
    Use this class when you want to load all the data and embeddings
    at once.
    """
    data: pl.DataFrame

    def __post_init__(self):
        # assert some useful properties on the CSV
        assert "label" in self.data.columns, "Missing label column"
        # modify the labels to be 0-indexed
        present_labels = self.data["label"].unique().to_list()
        # assert ascending order
        present_labels.sort()
        mappings = {label: i for i, label in enumerate(present_labels)}
        should_apply_mappings = any(k != v for k, v in mappings.items())
        if should_apply_mappings:
            print(f"Applying mappings: {mappings}")
            self.data = self.data.with_columns(pl.col(
                "label").replace(mappings))
        # drop the "type" column
        if "type" in self.data.columns:
            self.data.drop_in_place("type")
        self.n_non_feature_columns = 1
        self.number_of_classes = len(present_labels)

    @staticmethod
    def find_classnum_mapping(data: pl.DataFrame) -> Dict[str, int]:
        """
        Find the mapping of article types to class numbers.
        """
        article_types = data["type"].unique().to_list()
        # sort them, so we always guarantee the same mapping
        article_types.sort()
        return {article_type: i for i, article_type in enumerate(article_types)}

    @classmethod
    def from_raw_data(
        cls,
        raw_data: RawHumanChatBotData,
        embedder: ArticleEmbedder
    ) -> "HumanChatBotDataset":
        print(
            f"Generating embeddings for the dataset {raw_data} using embedder {embedder.__class__.__name__}")
        article_type_to_classnum = cls.find_classnum_mapping(raw_data.data)
        all_tensors = []
        all_labels = []
        all_types = []
        for index,row in enumerate(raw_data.data.iter_rows(named=True)):
            text = row["text"]
            article_type = row["type"]
            label = article_type_to_classnum[article_type]
            print(f"embedded row {index}")
            all_tensors.append(embedder.embed(text))
            all_labels.append(label)
            all_types.append(article_type)
        tensors_in_stack = torch.stack(all_tensors)
        samples, nfeatures = tensors_in_stack.shape
        assert samples == len(all_tensors)
        schema = [
            "f{}".format(i) for i in range(nfeatures)
        ]
        data = pl.DataFrame(
            {
                "type": all_types,
                "label": all_labels,
                **{schema[i]: tensors_in_stack[:, i].numpy() for i in range(nfeatures)}
            }
        )
        return cls(data)

    def get_samples_as_X(self) -> pl.DataFrame:
        return self.data.select(pl.col("*").exclude(["label"]))

    def get_sample_labels(self) -> np.ndarray:
        return self.data.select("label").to_numpy().ravel()

    @classmethod
    def from_train_test_raw_data(
        cls,
        train_data: RawHumanChatBotData,
        test_data: RawHumanChatBotData,
        embedder: ArticleEmbedder
    ) -> Tuple["HumanChatBotDataset", "HumanChatBotDataset"]:
        train = cls.from_raw_data(
            train_data,
            embedder
        )
        test = cls.from_raw_data(
            test_data,
            embedder
        )
        return train, test

    @classmethod
    def load(cls, load_path: str) -> "HumanChatBotDataset":
        """Reads the dataset from the csv file

        Args:
            load_path (str): the path of the csv file

        Returns:
            HumanChatBotDataset: The dataset
        """
        data = pl.read_csv(load_path)
        return cls(data)

    def save(self, save_path: str):
        """Save the dataset to a csv file

        Args:
            save_path (str): the path to save the dataset
        """
        self.data.write_csv(save_path)

    @property
    def embedding_size(self) -> int:
        # find the number of columns in the dataframe
        # because the first two columns are the type and the label
        return len(self.data.columns) - self.n_non_feature_columns

    def __len__(self) -> int:
        return len(self.data)

    def __getitem__(self, idx: int) -> Tuple[torch.Tensor, int]:
        tensor_values = self.data.row(idx)[self.n_non_feature_columns:]
        embedding = torch.tensor(tensor_values, dtype=torch.float32)
        label = self.data[idx]["label"].item()
        return embedding, label
    

def get_embedding_as_image_tensor(embedding: torch.Tensor) -> torch.Tensor:
    vector_size = len(embedding)
    embedding = embedding.view(1, vector_size)
    # These images are black and white, but the vectors might have small features
    # normalize each vector to be between 0 and 1
    embedding = (embedding - embedding.min()) / (embedding.max() - embedding.min())
    # calculate the cross multiplication
    cross_mult = embedding.T @ embedding
    # scale up the cross multiplication to be between 0 and 255
    cross_mult = cross_mult * 255
    # reshape to allow for the concept of a channel
    cross_mult = cross_mult.view(1, vector_size, vector_size)
    return cross_mult

normalization_transform = transforms.Normalize((0.5,), (0.5,))
tensor_to_pil = transforms.ToPILImage()
DEFAULT_IMAGE_SHAPE = (100, 100)
resize_transform = transforms.Resize(DEFAULT_IMAGE_SHAPE)


def generate_and_save(embedding, save_path):
    cross_mult = get_embedding_as_image_tensor(embedding)
    image = tensor_to_pil(cross_mult)
    image.save(save_path)

@dataclass
class ImageDataset(ABC, Dataset):

    @property
    @abstractmethod
    def image_height(self) -> int:
        pass

    @property
    @abstractmethod
    def image_width(self) -> int:
        pass

    @property
    @abstractmethod
    def number_of_classes(self) -> int:
        pass


@dataclass
class ImageByCrossMultiplicationDataset(ImageDataset):
    """This dataset contains embeddings expressed as the
    element wise multiplication of the same embedding.
    """
    human_chatbot_ds: HumanChatBotDataset
    resize: bool = True
    precompute: bool = False
    cache: Dict[int, torch.Tensor] = field(default_factory=dict, init=False)

    def __post_init__(self):
        if self.resize:
            self._image_height, self._image_width = DEFAULT_IMAGE_SHAPE
        else:
            self._image_height = self.human_chatbot_ds.embedding_size
            self._image_width = self.human_chatbot_ds.embedding_size

        if self.precompute:
            print("Precomputing images")
            for i in tqdm(range(len(self.human_chatbot_ds))):
                embedding, label = self.human_chatbot_ds[i]
                image = get_embedding_as_image_tensor(embedding)
                if self.resize:
                    image = resize_transform(image)
                self.cache[i] = image
            print("Precomputation completed")


    @classmethod
    def convert_and_save(cls, ds: HumanChatBotDataset, root_save_path: str):
        """Convert the dataset to an image dataset and save it to a file.

        Args:
            ds (HumanChatBotDataset): the dataset to convert
            save_path (str): the path to save the dataset
        """
        n_classes = ds.number_of_classes
        # make one directory for each class
        for i in range(n_classes):
            class_dir = os.path.join(root_save_path, str(i))
            os.makedirs(class_dir, exist_ok=True)
        print(
            f"Generating images from embeddings and saving at: {root_save_path!r}")

        for i in tqdm(range(len(ds))):
            embedding, label = ds[i]
            save_path = os.path.join(root_save_path, str(label), f"{i}.png")
            generate_and_save(embedding, save_path)
        print("Image generation tasks completed")

    def __len__(self) -> int:
        return len(self.human_chatbot_ds)
    
    def __getitem__(self, idx: int) -> Tuple[torch.Tensor, int]:
        embedding, label = self.human_chatbot_ds[idx]
        if self.cache:
            return self.cache[idx], label
        image = get_embedding_as_image_tensor(embedding)
        if self.resize:
            image = resize_transform(image)
        return image, label
    
    @property
    def image_height(self) -> int:
        return self._image_height
    
    @property
    def image_width(self) -> int:
        return self._image_width
    
    @property
    def number_of_classes(self) -> int:
        return self.human_chatbot_ds.number_of_classes


class ImageFolderDataset(ImageFolder):

    def __init__(self, root: str):
        transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Grayscale(),
            normalization_transform
        ])
        super().__init__(root, transform=transform)
        self.number_of_classes = len(self.classes)
        some_image = self[0][0]
        _, h, w = some_image.shape
        print(f"Loaded ImageFolder dataset {root!r} with {len(self)} samples. Image dimensions: {h}x{w}")
        self.image_height, self.image_width = h, w


@dataclass
class LazyHumanChatBotDataset(Dataset):
    """
    A PyTorch Dataset class for the Human Chat Bot dataset.
    Use this class when you want to lazily embed
    data on the fly.

    Useful when there's a lot of data and creating all the
    embeddings at once would be too memory intensive.
    """
    data: pl.DataFrame
    embedder: ArticleEmbedder = field(repr=False)
    article_type_to_classnum: Dict[str, int]

    def __post_init__(self):
        # assert that all types in the dataset
        # have a corresponding article type mapping
        types_in_dataset = self.data["type"].unique().to_list()
        missing_types = set(types_in_dataset) - \
            set(self.article_type_to_classnum.keys())
        if missing_types:
            raise ValueError(
                f"Missing classnumber for types: {missing_types}")

    def get_class_num(self, article_type: str) -> int:
        """
        Get the class number for the given article type.
        """
        return self.article_type_to_classnum[article_type]

    @classmethod
    def from_raw_data(
        cls,
        raw_data: RawHumanChatBotData,
        embedder: ArticleEmbedder,
        article_type_to_classnum: Dict[str, int]
    ) -> "LazyHumanChatBotDataset":
        return cls(
            raw_data.data,
            embedder,
            article_type_to_classnum
        )

    def __len__(self) -> int:
        return len(self.data)

    def __getitem__(self, idx: int) -> Tuple[np.ndarray, int]:
        row = self.data[idx]
        text = row["text"].item()
        article_type = row["type"].item()
        label = self.get_class_num(article_type)
        return self.embedder.embed(text), label
