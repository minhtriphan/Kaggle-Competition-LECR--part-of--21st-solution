# Kaggle-Competition-LECR--part-of--21st-solution
This is a part of the 21st position in the Learning Equality - Curriculum Recommendation competition on Kaggle (https://www.kaggle.com/competitions/learning-equality-curriculum-recommendations)

The entire model (retriever and re-ranker) gets 0.610/0.645 on public/private LB. This model alone can be the 40th/1057 teams in the competition.

The structure of the retriever and the re-ranker are given in the below images

* The retriever
![LECR-Retriever](https://user-images.githubusercontent.com/39737967/226330465-72a8de2a-ad07-4cdc-9fa7-c36c99fd401a.jpg)

* The re-ranker
![LECR-Reranker](https://user-images.githubusercontent.com/39737967/226330454-0e9f8de4-af40-4ffc-ad4b-c8db16068ade.jpg)

# Instruction to run the code
1. Clone the repo;
2. Download the datasets 
* The competition dataset: https://www.kaggle.com/competitions/learning-equality-curriculum-recommendations/data
* The extra dataset: https://www.kaggle.com/datasets/shinomoriaoshi/lecrext-data
* Fined-tune multi-linguistic BERT: https://www.kaggle.com/datasets/utm529fg/paraphrasemultilingualmpnetbasev3
If you do it via Kaggle API, please use
```
!kaggle competitions download -c learning-equality-curriculum-recommendations
!kaggle datasets download -d shinomoriaoshi/lecrext-data
!kaggle datasets download -d utm529fg/paraphrasemultilingualmpnetbasev3
```
Then, unzip and move them to the repo
```
!unzip learning-equality-curriculum-recommendations -d data
!unzip paraphrasemultilingualmpnetbasev3
!unzip lecrext-data -d ext_data

!mv data Kaggle-Competition-LECR--part-of--21st-solution
!mv ext_data Kaggle-Competition-LECR--part-of--21st-solution
!mv paraphrase-multilingual-mpnet-base-v4 Kaggle-Competition-LECR--part-of--21st-solution
!cd Kaggle-Competition-LECR--part-of--21st-solution
```

3. Start training the retriever
`!python retriever_train.py`

4. After training the retriever, train the re-ranker
`!python reranker_train.py`

There could be some (or even a lot) of bugs, please reach out to me at phanminhtri2611@gmail.com for detailed discussions.
