python3 -W ignore train_wc.py --train_file data_bioner_5/BC4CHEMD-IOBES/merge.tsv \
			      --dev_file data_bioner_5/BC4CHEMD-IOBES/devel.tsv \
			      --test_file data_bioner_5/BC4CHEMD-IOBES/test.tsv \
			      --caseless --fine_tune --emb_file data_bioner_5/wikipedia-pubmed-and-PMC-w2v.txt \
			      --word_dim 200 --gpu 1 --shrink_embedding --patience 30 --epoch 100 --checkpoint ./checkpoint/bilstm/
