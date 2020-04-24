import sys

from transformers import AutoTokenizer

dataset = sys.argv[1]
model_name_or_path = sys.argv[2]
max_len = int(sys.argv[3])

subword_len_counter = 0

tokenizer = AutoTokenizer.from_pretrained(model_name_or_path)

with open(dataset, "rt") as f_p:
    for line in f_p:
        line = line.rstrip()
        # end of example
        if not line:
            print(line)
            subword_len_counter = 0
            continue

        line = line.split()
        token, label = line[0], line[-1]

        current_subwords_len = len(tokenizer.tokenize(token))
        # Token contains strange control characters like \x96 or \x95
        # Just filter out the complete line
        if current_subwords_len == 0:
            continue

        if (subword_len_counter + current_subwords_len) > max_len:
            print("")
            print(token, label)
            subword_len_counter = 0
            continue

        subword_len_counter += current_subwords_len

        print(token, label)