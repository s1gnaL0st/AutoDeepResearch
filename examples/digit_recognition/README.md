# Handwritten digit recognition demo

This small reproducible task uses the built-in `sklearn.datasets.load_digits`
dataset (8×8 grayscale images, 10 classes). The baseline is multinomial
logistic regression; the candidate is an RBF SVM. Both scripts emit the metric
schema required by AutoResearch.

