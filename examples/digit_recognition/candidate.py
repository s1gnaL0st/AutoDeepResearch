"""Candidate: RBF SVM for handwritten digit recognition."""
from sklearn.datasets import load_digits
from sklearn.svm import SVC
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score
import json

X, y = load_digits(return_X_y=True)
X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.25, random_state=42, stratify=y
)
model = SVC(C=10.0, gamma="scale", kernel="rbf", random_state=42)
model.fit(X_train, y_train)
print(json.dumps({"metrics": {"accuracy": float(accuracy_score(y_test, model.predict(X_test)))}}))
