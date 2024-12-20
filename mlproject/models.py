import torch

from functools import partial
from typing import Type, List, ClassVar, Dict, Tuple
from torch.optim import Optimizer
from torch.utils.data import DataLoader

from collections import defaultdict
from contextlib import contextmanager

from abc import ABC, abstractmethod

from mlproject.datasets import HumanChatBotDataset
from mlproject.data_processing import NeuralNetworkExperimentResult


from tqdm import tqdm


class NNBaseModel(torch.nn.Module, ABC):
    registry: ClassVar[Dict[str, "NNBaseModel"]] = {}

    def __init__(self):
        super(NNBaseModel, self).__init__()

    def __init_subclass__(cls) -> None:
        if cls.__name__ not in NNBaseModel.registry:
            NNBaseModel.registry[cls.__name__] = cls
        return super().__init_subclass__()

    @abstractmethod
    def run_training(
        self,
        train_dataset: HumanChatBotDataset,
        test_dataset: HumanChatBotDataset,
        learning_rate: float = 0.001,
        epochs: int = 100,
    ):
        pass

    def compute_accuracy(self, data_loader: DataLoader) -> Tuple[int, int, float, Dict[int, Dict[int, int]]]:
        correct = 0
        make_up = defaultdict(lambda: defaultdict(int))
        with torch.no_grad():
            for text_vectors, text_labels in data_loader:
                text_vectors = text_vectors.to(self.device)
                text_labels = text_labels.to(self.device)
                outputs = self(text_vectors)
                normalized_outputs = torch.softmax(outputs, dim=1)
                predicted = torch.argmax(normalized_outputs, dim=1)
                correct += (predicted == text_labels).sum().item()
                # The predicted make "the buckets"
                # the text_labels is the "true" class
                for _i, _j in zip(predicted, text_labels):
                    if isinstance(_i, torch.Tensor):
                        i = _i.item()
                    else:
                        i = int(_i)

                    if isinstance(_j, torch.Tensor):
                        j = _j.item()
                    else:
                        j = int(_j)
                    make_up[i][j] += 1
        total_items = len(data_loader.dataset)
        accuracy = correct / total_items
        return correct, total_items, accuracy, make_up

    def compute_loss(self, data_loader: DataLoader, criterion: torch.nn.Module) -> float:
        loss = 0.0
        with torch.no_grad():
            for text_vectors, text_labels in data_loader:
                text_vectors = text_vectors.to(self.device)
                text_labels = text_labels.to(self.device)
                outputs = self(text_vectors)
                # CrossEntropyLoss gives you the mean loss
                # of the batch already, so the only thing we have
                # to do is divide by the number of batches
                # later.
                loss += criterion(outputs, text_labels).item()
        loss /= len(data_loader)
        return loss

    def to(self, device: str):
        self.device = device
        return super().to(device)
    
    @contextmanager
    def validation_context(self):
        try:
            self.eval()
            yield
        finally:
            self.train()


    def validate_after_epoch(
        self,
        epoch: int,
        train_loader: DataLoader,
        test_loader: DataLoader,
        criterion: torch.nn.Module,
        exp_result: NeuralNetworkExperimentResult
    ):
        with self.validation_context():
            self._validate_after_epoch(
                epoch,
                train_loader,
                test_loader,
                criterion,
                exp_result
            )

    def _validate_after_epoch(
            self,
            epoch: int,
            train_loader: DataLoader,
            test_loader: DataLoader,
            criterion: torch.nn.Module,
            exp_result: NeuralNetworkExperimentResult
    ):
        print(f"Validating after epoch: {epoch}")
        training_losses = exp_result.training_losses
        testing_losses = exp_result.testing_losses
        training_accuracies = exp_result.training_accuracies
        testing_accuracies = exp_result.testing_accuracies
        training_loss = self.compute_loss(train_loader, criterion)
        training_losses.append(training_loss)
        _, _, training_accuracy, tr_make_up = self.compute_accuracy(
            train_loader)
        training_accuracies.append(training_accuracy)
        testing_loss = self.compute_loss(test_loader, criterion)
        testing_losses.append(testing_loss)
        _, _, testing_accuracy, test_make_up = self.compute_accuracy(
            test_loader)
        testing_accuracies.append(testing_accuracy)
        exp_result.training_classification_results.append(tr_make_up)
        exp_result.testing_classification_results.append(test_make_up)
        print(f"Epoch: {epoch}, Training Loss: {training_loss}, Training Accuracy: {training_accuracy*100:.2f}%, Testing Loss: {testing_loss}, Testing Accuracy: {testing_accuracy*100:.2f}%")


class LogisticRegression(NNBaseModel):
    """A simple neural network logistic regression model.
    Different than a MLP, this model has no hidden layers.

    NOT TO BE CONFUSED WITH THE SCIKIT-LEARN LOGISTIC REGRESSION MODEL.
    """

    def __init__(self, input_dim: int, output_dim: int, learning_rate: float = 0.001):
        super(LogisticRegression, self).__init__()
        self.linear = torch.nn.Linear(input_dim, output_dim)
        self.optimizer = torch.optim.SGD(self.parameters(), lr=learning_rate)
        self.criterion = torch.nn.CrossEntropyLoss()
        self.criterion_name = f"{self.criterion.__class__.__name__}"
        self.optimizer_name = f"{self.optimizer.__class__.__name__}"
        self.learning_rate = learning_rate

    def forward(self, x):
        return torch.relu(self.linear(x))

    def run_training(
        self,
        train_dataset: HumanChatBotDataset,
        test_dataset: HumanChatBotDataset,
        epochs: int
    ) -> NeuralNetworkExperimentResult:
        
        training_batch_size = 32
        train_loader = DataLoader(
            train_dataset, batch_size=training_batch_size, shuffle=True)
        test_loader = DataLoader(test_dataset, batch_size=32, shuffle=False)
        print(
            f"Proceeding to train {self.__class__.__name__} for {epochs} epochs...")
        exp_result = NeuralNetworkExperimentResult(
            learning_rate=self.learning_rate,
            training_batch_size=training_batch_size,
            optimizer_name=self.optimizer_name,
            criterion_name=self.criterion_name,
            epochs=epochs
        )
        optimizer = self.optimizer
        criterion = self.criterion
        for epoch in tqdm(range(epochs)):
            for _, (text_vectors, text_labels) in enumerate(train_loader):
                text_vectors = text_vectors.to(self.device)
                text_labels = text_labels.to(self.device)
                optimizer.zero_grad()
                outputs = self(text_vectors)
                loss = criterion(outputs, text_labels)
                loss.backward()
                optimizer.step()
            self.validate_after_epoch(
                epoch,
                train_loader,
                test_loader,
                criterion,
                exp_result
            )
        return exp_result


class SimpleMLP(NNBaseModel):
    """A MLP with a single hidden layer of 128 neurons.
    """

    def __init__(self, input_dim: int, output_dim: int, learning_rate: float = 0.001):
        super(SimpleMLP, self).__init__()
        self.fc1 = torch.nn.Linear(input_dim, 128)
        self.fc2 = torch.nn.Linear(128, output_dim)
        self.optimizer = torch.optim.Adam(self.parameters(), lr=learning_rate)
        self.criterion = torch.nn.CrossEntropyLoss()
        self.learning_rate = learning_rate
        self.optimizer_name = f"{self.optimizer.__class__.__name__}"
        self.criterion_name = f"{self.criterion.__class__.__name__}"

    def forward(self, x):
        x = torch.relu(self.fc1(x))
        x = torch.softmax(self.fc2(x), dim=1)
        return x

    def run_training(
        self,
        train_dataset: HumanChatBotDataset,
        test_dataset: HumanChatBotDataset,
        epochs: int
    ):
        optimizer = self.optimizer
        criterion = self.criterion
        train_loader = DataLoader(train_dataset, batch_size=32, shuffle=True)
        test_loader = DataLoader(test_dataset, batch_size=32, shuffle=False)
        print(
            f"Proceeding to train {self.__class__.__name__} for {epochs} epochs...")
        exp_result = NeuralNetworkExperimentResult(
            learning_rate=self.learning_rate,
            optimizer_name=self.optimizer_name,
            criterion_name=self.criterion_name,
            epochs=epochs,
            training_batch_size=32
        )
        for epoch in tqdm(range(epochs)):
            for _, (text_vectors, text_labels) in enumerate(train_loader):
                text_vectors = text_vectors.to(self.device)
                text_labels = text_labels.to(self.device)
                optimizer.zero_grad()
                outputs = self(text_vectors)
                loss = criterion(outputs, text_labels)
                loss.backward()
                optimizer.step()

            self.validate_after_epoch(
                epoch,
                train_loader,
                test_loader,
                criterion,
                exp_result
            )
        return exp_result


def get_cnn_image_dimensions(
        image_height: int,
        image_width: int,
        padding: int,
        kernel_size: int,
        stride: int,
        pool_stride: int = 2
    ) -> Tuple[int, int]:
    """Calcule the new height and width of an image after a convolutional layer.

    Args:
        image_height (int): input image height
        image_width (int): input image width
        padding (int): the padding used for the kernels
        kernel_size (int): the kernel size
        stride (int): the stride applied at the convolutional step
        pool_stride (int, optional): the stride used for any pooling layer. Defaults to 2.

    Returns:
        Tuple[int, int]: new_height, new_width
    """
    new_height = ((image_height - kernel_size + 2*padding) // stride) + 1
    new_width = ((image_width - kernel_size + 2*padding) // stride) + 1
    return new_height//pool_stride, new_width//pool_stride


class CNN2D(NNBaseModel):

    def __init__(self, image_height: int, image_width: int, n_classes: int, kernel_size: int = 3, learning_rate: float = 0.001):
        # TODO: Conside expanding the neural network with batchnorm
        # dropout, and the like.
        super(CNN2D, self).__init__()
        # define two convolutional blocks, followed by a fully connected
        # neural network. First block, 3 channels, 3x3 kernel, stride 1
        # second block, 6 channels, 5x5 kernel, stride 1
        self.conv1, new_dims_after_one = self.convolutional_block(
            in_channels=1, out_channels=3, kernel_size=kernel_size, stride=1)
        self.conv2, new_dims_after_second = self.convolutional_block(
            in_channels=3, out_channels=6, kernel_size=5, stride=1)
        
        final_h, final_w = new_dims_after_second(*new_dims_after_one(image_height=image_height, image_width=image_width))
        flatten_output_dim = 6 * final_h * final_w
        self.fc = self.sequential_network(flatten_output_dim, 32, n_classes)
        self.optimizer = torch.optim.Adam(self.parameters(), lr=learning_rate)
        self.criterion = torch.nn.CrossEntropyLoss()
        self.learning_rate = learning_rate
        self.criterion_name = f"{self.criterion.__class__.__name__}"
        self.optimizer_name = f"{self.optimizer.__class__.__name__}"

    def forward(self, x):
        x = self.conv1(x)
        x = self.conv2(x)
        x = torch.flatten(x, 1)
        x = self.fc(x)
        x = torch.softmax(x, dim=1)
        return x
    
    def convolutional_block(
            self,
            in_channels: int,
            out_channels: int,
            kernel_size: int,
            stride: int = 1
        ) -> Tuple[torch.nn.Module, callable]:
        nn = torch.nn.Sequential(
            torch.nn.Conv2d(in_channels=in_channels, out_channels=out_channels, kernel_size=kernel_size, stride=stride),
            torch.nn.Sigmoid(),
            torch.nn.MaxPool2d(kernel_size=2, stride=2),
            torch.nn.Dropout(0.3)
        )
        new_dim_calc = partial(get_cnn_image_dimensions,
                               kernel_size=kernel_size, stride=stride, padding=0, pool_stride=2)
        return nn, new_dim_calc
    
    def sequential_network(self, input_dim: int, hidden_dim: int, output_dim: int):
        return torch.nn.Sequential(
            torch.nn.Linear(input_dim, hidden_dim),
            torch.nn.Sigmoid(),
            torch.nn.Linear(hidden_dim, output_dim)
        )


    def run_training(
        self,
        train_dataset: HumanChatBotDataset,
        test_dataset: HumanChatBotDataset,
        epochs: int = 100,
    ):
        training_batch_size = 32
        train_loader = DataLoader(
            train_dataset, batch_size=training_batch_size, shuffle=True)
        test_loader = DataLoader(
            test_dataset, batch_size=32, shuffle=False
        )
        exp_result = NeuralNetworkExperimentResult(
            learning_rate=self.learning_rate,
            training_batch_size=training_batch_size,
            criterion_name=self.criterion_name,
            optimizer_name=self.optimizer_name,
            epochs=epochs
        )
        optimizer = self.optimizer
        criterion = self.criterion
        for epoch in tqdm(range(epochs)):
            for text_vectors, text_labels in train_loader:
                text_vectors = text_vectors.to(self.device)
                text_labels = text_labels.to(self.device)
                optimizer.zero_grad()
                outputs = self(text_vectors)
                loss = criterion(outputs, text_labels)
                loss.backward()
                optimizer.step()
            self.validate_after_epoch(
                epoch,
                train_loader,
                test_loader,
                criterion,
                exp_result
            )
        return exp_result

"""
class CNNLstm(NNBaseModel):

    def __init__(
            self,
            image_height: int,
            image_width: int,
            n_classes: int,
            kernel_size: int = 3,
            out_channels: int = 3,
            learning_rate: float = 0.001
        ):
        super(CNNLstm, self).__init__()
        self.cnn = torch.nn.Sequential(
            torch.nn.Conv2d(1, out_channels, kernel_size=kernel_size),
            torch.nn.MaxPool2d(kernel_size=2, stride=2),
            torch.nn.ReLU()
        )
        self.lstm_hidden_size = 128
        self.lstm = torch.nn.LSTM(
            input_size=out_channels *
            (image_height - kernel_size + 1) *
            (image_width - kernel_size + 1) // 4,
            hidden_size=self.lstm_hidden_size,
            num_layers=1,
            batch_first=True
        )
        self.fc = torch.nn.Linear(self.lstm_hidden_size, n_classes)
        self.optimizer = torch.optim.Adam(self.parameters(), lr=learning_rate)
        self.criterion = torch.nn.CrossEntropyLoss()
        self.learning_rate = learning_rate
        self.criterion_name = f"{self.criterion.__class__.__name__}"
        self.optimizer_name = f"{self.optimizer.__class__.__name__}"

    def forward(self, x):
        x = self.cnn(x)
        x = x.view(x.size(0), -1)
        x = x.unsqueeze(1)
        x, _ = self.lstm(x)
        x = torch.softmax(self.fc(x[:, -1, :]),  dim=1)
        return x

    def run_training(
        self,
        train_dataset: HumanChatBotDataset,
        test_dataset: HumanChatBotDataset,
        epochs: int = 100,
    ):
        optimizer = self.optimizer
        criterion = self.criterion
        training_batch_size = 32
        train_loader = DataLoader(
            train_dataset, batch_size=training_batch_size, shuffle=True)
        test_loader = DataLoader(
            test_dataset, batch_size=32, shuffle=False
        )
        exp_result = NeuralNetworkExperimentResult(
            learning_rate=self.learning_rate,
            training_batch_size=training_batch_size,
            criterion_name=self.criterion_name,
            optimizer_name=self.optimizer_name,
            epochs=epochs
        )
        for epoch in tqdm(range(epochs)):
            for text_vectors, text_labels in train_loader:
                text_vectors = text_vectors.to(self.device)
                text_labels = text_labels.to(self.device)
                optimizer.zero_grad()
                outputs = self(text_vectors)
                loss = criterion(outputs, text_labels)
                loss.backward()
                optimizer.step()
            self.validate_after_epoch(
                epoch,
                train_loader,
                test_loader,
                criterion,
                exp_result
            )
        return exp_result
"""
class CNNLstm(NNBaseModel):

    def __init__(
        self,
        image_height: int,
        image_width: int,
        n_classes: int,
        kernel_size: int = 3,
        out_channels: int = 16,
        learning_rate: float = 0.001,
    ):
        super(CNNLstm, self).__init__()
        self.cnn = torch.nn.Sequential(
            torch.nn.Conv2d(1, out_channels, kernel_size=kernel_size),
            torch.nn.ReLU(),
            torch.nn.MaxPool2d(kernel_size=2, stride=2),
            torch.nn.Dropout(0.3)  # Dropout to prevent overfitting
        )
        cnn_output_dim = (
            (image_height - kernel_size + 1) // 2,
            (image_width - kernel_size + 1) // 2,
        )
        self.cnn_flatten_dim = out_channels * cnn_output_dim[0] * cnn_output_dim[1]
        self.lstm_hidden_size = 128
        self.lstm = torch.nn.LSTM(
            input_size=self.cnn_flatten_dim,
            hidden_size=self.lstm_hidden_size,
            num_layers=2,  # Increased depth for better representation
            batch_first=True,
            dropout=0.3  # Dropout for LSTM layers
        )
        self.fc = torch.nn.Sequential(
            torch.nn.Linear(self.lstm_hidden_size, 64),
            torch.nn.ReLU(),
            torch.nn.Dropout(0.3),
            torch.nn.Linear(64, n_classes)
        )
        self.optimizer = torch.optim.Adam(self.parameters(), lr=learning_rate)
        self.criterion = torch.nn.CrossEntropyLoss()
        self.learning_rate = learning_rate
        self.criterion_name = f"{self.criterion.__class__.__name__}"
        self.optimizer_name = f"{self.optimizer.__class__.__name__}"

    def forward(self, x):
        x = self.cnn(x)
        x = x.view(x.size(0), -1)  # Flatten CNN output
        x = x.unsqueeze(1)  # Add sequence dimension for LSTM
        x, _ = self.lstm(x)  # LSTM output
        x = self.fc(x[:, -1, :])  # Use the last LSTM output
        x = torch.softmax(x, dim=1)
        return x

    def run_training(
            self,
            train_dataset: HumanChatBotDataset,
            test_dataset: HumanChatBotDataset,
            epochs: int = 100,
            learning_rate: float = 0.001  # Added this parameter
    ):
        optimizer = self.optimizer
        criterion = self.criterion
        training_batch_size = 32
        train_loader = DataLoader(
            train_dataset, batch_size=training_batch_size, shuffle=True
        )
        test_loader = DataLoader(
            test_dataset, batch_size=32, shuffle=False
        )
        exp_result = NeuralNetworkExperimentResult(
            learning_rate=self.learning_rate,  # Use self.learning_rate or the passed argument
            training_batch_size=training_batch_size,
            criterion_name=self.criterion_name,
            optimizer_name=self.optimizer_name,
            epochs=epochs
        )
        for epoch in range(epochs):
            for text_vectors, text_labels in train_loader:
                text_vectors = text_vectors.to(self.device)
                text_labels = text_labels.to(self.device)
                optimizer.zero_grad()
                outputs = self(text_vectors)
                loss = criterion(outputs, text_labels)
                loss.backward()
                optimizer.step()
            self.validate_after_epoch(
                epoch,
                train_loader,
                test_loader,
                criterion,
                exp_result
            )
        return exp_result